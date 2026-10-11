#!/usr/bin/env python3
"""Compare INT8 and full-precision rankings on a retained recording, entirely locally."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from birdcast_scraper import atomic_write_json
from recording_store import RecordingStore


def compare(root: Path, identifier: str) -> dict:
    metadata, _ = RecordingStore(root / "recordings").read(identifier)
    results = []
    for compressed in (True, False):
        command = [sys.executable, str(Path(__file__).with_name("animal_worker.py")),
                   "--assets", str(root / ".models" / "perch-v2"),
                   "--recordings", str(root / "recordings")]
        if compressed:
            command.append("--int8")
        process = subprocess.run(command,
                                 input=json.dumps({"recording_id": identifier, "sha256": metadata["sha256"]}) + "\n",
                                 capture_output=True, text=True, timeout=180,
                                 env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
        messages = [json.loads(line) for line in process.stdout.splitlines()]
        if process.returncode or len(messages) != 2 or any("error" in message for message in messages):
            raise RuntimeError(f"Model comparison failed: {messages or process.stderr}")
        results.append(messages[1])
    windows = []
    for compressed, reference in zip(results[0]["windows"], results[1]["windows"]):
        names = lambda window: [item["scientific_name"] for item in window["candidates"]]
        compressed_names, reference_names = names(compressed), names(reference)
        windows.append({"start_seconds": compressed["start_seconds"],
                        "same_top_1": compressed_names[0] == reference_names[0],
                        "same_top_5_set": set(compressed_names) == set(reference_names),
                        "same_top_5_order": compressed_names == reference_names})
    return {"recording_id": identifier, "sha256": metadata["sha256"],
            "note": "Ranking agreement is not verification of species presence.",
            "windows": windows, "int8": results[0], "full_precision": results[1]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording_id")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = compare(args.root.resolve(), args.recording_id)
    if args.output:
        atomic_write_json(args.output, report)
    print(json.dumps({key: value for key, value in report.items() if key not in ("int8", "full_precision")}, indent=2))
