#!/usr/bin/env python3
"""Measure local Perch latency, memory and HTTP responsiveness without opening sensors."""
from __future__ import annotations

import argparse
import io
import json
import statistics
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

from animal_analysis import AnalysisCoordinator
from animal_model import IDENTITY
from birdcast_scraper import atomic_write_json
from birdcast_service import Service, build_server
from recording_store import RecordingStore


def p95(values: list[float]) -> float:
    return sorted(values)[min(len(values) - 1, int(.95 * len(values)))]


def benchmark(root: Path, recording_id: str | None, repeats: int) -> dict:
    if recording_id:
        _, data = RecordingStore(root / "recordings").read(recording_id)
        fixture = "existing recording, not manually labeled"
    else:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\0\0" * 480000)
        data = buffer.getvalue()
        fixture = "synthetic silence; performance only"
    with tempfile.TemporaryDirectory(prefix="perch-benchmark-") as directory:
        service = Service(Path(directory), Path(directory) / "config.json", animal_analysis=False)
        analyzer = AnalysisCoordinator(Path(directory), service.recording_store)
        analyzer.assets = root / ".models" / "perch-v2"
        service.animal_analysis = analyzer
        if unavailable := analyzer.availability():
            raise RuntimeError(unavailable)
        service.recording_dir.mkdir()
        metadata = []
        for index in range(repeats + 1):
            identifier = f"{index + 1:032x}"
            service.recording_store.path(identifier).write_bytes(data)
            item, _ = service.recording_store.read(identifier)
            service.recording_store.publish_metadata(item)
            metadata.append(item)
        server = build_server(service, host="127.0.0.1", port=0)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        sensor_stop = threading.Event()

        def synthetic_frames():
            while not sensor_stop.is_set():
                with service.presence_sensor.lock:
                    service.presence_sensor.connected = True
                    service.presence_sensor.received_at = time.monotonic()
                    service.presence_sensor.latest = {"presence": "moving", "timestamp": "synthetic"}
                sensor_stop.wait(.05)

        feeder = threading.Thread(target=synthetic_frames, daemon=True)
        feeder.start()
        memory_kib = 0

        def sample():
            started = time.monotonic()
            with urlopen(base + "/v1/latest", timeout=5) as response:
                latest = json.load(response)
            elapsed = (time.monotonic() - started) * 1000
            return elapsed, latest["sources"]["radar"]["result"]["fresh"]

        baseline, loaded = [], []
        baseline_stale = loaded_stale = 0
        try:
            for _ in range(20):
                elapsed, fresh = sample()
                baseline.append(elapsed)
                baseline_stale += not fresh
                time.sleep(.02)
            timings = []
            for item in metadata:
                analyzer.request(item)
                deadline = time.monotonic() + 180
                while True:
                    state = analyzer.status(item)
                    if state["state"] == "completed":
                        timings.append(state["processing_seconds"])
                        break
                    if state["state"] not in ("queued", "running"):
                        raise RuntimeError(state.get("error", "Analysis failed"))
                    if time.monotonic() > deadline:
                        raise TimeoutError("Benchmark timed out")
                    elapsed, fresh = sample()
                    loaded.append(elapsed)
                    loaded_stale += not fresh
                    process = analyzer._process
                    if process:
                        try:
                            for line in Path(f"/proc/{process.pid}/status").read_text().splitlines():
                                if line.startswith("VmHWM:"):
                                    memory_kib = max(memory_kib, int(line.split()[1]))
                        except OSError:
                            pass
                    time.sleep(.03)
            return {"measured_at": datetime.now(timezone.utc).isoformat(),
                    "hardware": Path("/proc/device-tree/model").read_text().rstrip("\0"),
                    "fixture": fixture, "model": IDENTITY, "inference_threads": 1, "priority_nice": 10,
                    "cold_job_seconds": timings[0], "warm_job_seconds": timings[1:],
                    "warm_median_seconds": statistics.median(timings[1:]),
                    "worker_peak_rss_mib": memory_kib / 1024,
                    "http_baseline_p95_ms": p95(baseline), "http_loaded_p95_ms": p95(loaded),
                    "radar_mode": "synthetic 20 Hz frames; no physical UART access",
                    "baseline_stale_samples": baseline_stale, "loaded_stale_samples": loaded_stale}
        finally:
            analyzer.stop()
            sensor_stop.set()
            feeder.join(timeout=1)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--recording-id", help="Analyze a validated recording locally; it is never uploaded.")
    parser.add_argument("--repeats", type=int, default=3, help="Number of warm jobs after one cold job")
    parser.add_argument("--output", type=Path, help="Optional JSON performance report")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("--repeats must be between 1 and 20")
    report = benchmark(args.root.resolve(), args.recording_id, args.repeats)
    if args.output:
        atomic_write_json(args.output, report)
    print(json.dumps(report, indent=2))
