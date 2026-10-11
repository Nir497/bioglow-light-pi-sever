#!/usr/bin/env python3
"""Small standard-library HTTP boundary for BirdCast and Pi microphone audio."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import birdcast_scraper
from light_sensor import LightSensor

DEFAULT_PORT = 8000
DEFAULT_RECORDER = "/home/nir/record_microphone.py"
RECORDING_SECONDS = 10
RECORDING_RATE = 48_000
RECORDING_CHANNELS = 1
RECORDING_SAMPLE_WIDTH = 2
RECORDING_FRAMES = RECORDING_SECONDS * RECORDING_RATE
RECORDING_BYTES = RECORDING_FRAMES * RECORDING_CHANNELS * RECORDING_SAMPLE_WIDTH
SUPPORTED_REGIONS = {f"US-{code}" for code in birdcast_scraper.STATE_NAMES}


def error_payload(code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code, "message": message}}


class BusyError(Exception):
    pass


class RecordingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RecordingNotFound(RecordingError):
    def __init__(self):
        super().__init__("recording_not_found", "The requested recording is not available.")


class Service:
    def __init__(self, root: Path, config: Path, runner=None, recorder: str = DEFAULT_RECORDER):
        self.root = root
        self.light_sensor = LightSensor()
        self.config = config
        self.runner = runner or self._run_scraper
        self._lock = threading.Lock()
        self.recorder = recorder
        self.recording_dir = self.root / "recordings"
        self._recording_lock = threading.Lock()
        self._active_recording: dict[str, Any] | None = None
        self._transfer_active = False

    def snapshot(self, region: str) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise BusyError("Another BirdCast snapshot is already in progress.")
        try:
            return self.runner(region)
        finally:
            self._lock.release()

    def _run_scraper(self, region: str) -> dict[str, Any]:
        output = self.root / "birdcast_service_snapshot.json"
        config = self.root / "birdcast_service_config.json"
        config.write_text(json.dumps({"region": region, "output_file": str(output)}), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(self.root / "birdcast_scraper.py"), "--config", str(config), "--region", region],
            cwd=self.root, capture_output=True, text=True, timeout=45,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "BirdCast scraper failed")
        try:
            snapshot = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"scraper returned malformed JSON: {exc}") from exc
        if snapshot.get("status") != "ok":
            raise LookupError(f"BirdCast did not provide a current snapshot for {region}.")
        return snapshot

    def start_recording(self) -> dict[str, Any]:
        if not self._recording_lock.acquire(blocking=False):
            raise BusyError("Another recording or audio transfer is already in progress.")
        try:
            if self._active_recording is not None:
                raise BusyError("Another recording or audio transfer is already in progress.")
            self.recording_dir.mkdir(parents=True, exist_ok=True)
            recording_id = uuid.uuid4().hex
            expected = (self.recording_dir / f"{recording_id}.wav").resolve()
            try:
                completed = subprocess.run(
                    [sys.executable, self.recorder, "--output", str(expected)],
                    cwd=self.root, capture_output=True, text=True,
                    timeout=RECORDING_SECONDS + 30,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                )
            except subprocess.TimeoutExpired as exc:
                raise RecordingError("recorder_timeout", "Microphone recorder timed out.") from exc
            if completed.returncode != 0:
                raise RecordingError("recorder_failed", completed.stderr.strip() or "Microphone recorder failed.")
            printed = completed.stdout.strip().splitlines()
            if not printed or Path(printed[-1]).resolve() != expected:
                raise RecordingError("missing_output", "Recorder did not publish the expected output file.")
            metadata = self._validate_recording(expected, recording_id)
            self._active_recording = {"id": recording_id, "path": expected, "metadata": metadata}
            return metadata
        finally:
            self._recording_lock.release()

    def _validate_recording(self, path: Path, recording_id: str) -> dict[str, Any]:
        if not path.is_absolute() or path != path.resolve() or not path.is_file():
            raise RecordingError("missing_output", "The completed recording is missing.")
        if not os.access(path, os.R_OK):
            raise RecordingError("invalid_audio", "The completed recording is not readable.")
        try:
            with wave.open(str(path), "rb") as audio:
                params = audio.getparams()
                if (
                    params.nchannels != RECORDING_CHANNELS
                    or params.framerate != RECORDING_RATE
                    or params.sampwidth != RECORDING_SAMPLE_WIDTH
                    or params.comptype != "NONE"
                    or audio.getnframes() != RECORDING_FRAMES
                ):
                    raise RecordingError("invalid_audio", "Recording is not a 10-second mono 48 kHz PCM WAV.")
        except RecordingError:
            raise
        except (OSError, EOFError, wave.Error) as exc:
            raise RecordingError("invalid_audio", f"Recording WAV validation failed: {exc}") from exc
        byte_length = path.stat().st_size
        if byte_length != 44 + RECORDING_BYTES:
            raise RecordingError("invalid_audio", "Recording has an unexpected byte length.")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "status": "ok", "recording_id": recording_id, "content_type": "audio/wav",
            "duration_seconds": RECORDING_SECONDS, "sample_rate": RECORDING_RATE,
            "channels": RECORDING_CHANNELS, "bits_per_sample": RECORDING_SAMPLE_WIDTH * 8,
            "frame_count": RECORDING_FRAMES, "byte_length": byte_length, "sha256": digest,
            "download_path": f"/v1/recordings/{recording_id}",
        }

    def recording(self, recording_id: str) -> tuple[dict[str, Any], bytes]:
        with self._recording_lock:
            if self._transfer_active:
                raise BusyError("Another recording or audio transfer is already in progress.")
            self._transfer_active = True
        try:
            active = self._active_recording
            if active is None or active["id"] != recording_id:
                raise RecordingNotFound()
            try:
                data = active["path"].read_bytes()
            except OSError as exc:
                raise RecordingError("transfer_failure", f"Recording could not be read: {exc}") from exc
            if len(data) != active["metadata"]["byte_length"] or hashlib.sha256(data).hexdigest() != active["metadata"]["sha256"]:
                raise RecordingError("transfer_failure", "Recording changed while it was being transferred.")
            return active["metadata"], data
        except Exception:
            with self._recording_lock:
                self._transfer_active = False
            raise

    def finish_transfer(self) -> None:
        with self._recording_lock:
            self._transfer_active = False

    def delete_recording(self, recording_id: str) -> None:
        if not self._recording_lock.acquire(blocking=False):
            raise BusyError("Another recording or audio transfer is already in progress.")
        try:
            active = self._active_recording
            if self._transfer_active:
                raise BusyError("Another recording or audio transfer is already in progress.")
            if active is None or active["id"] != recording_id:
                raise RecordingNotFound()
            try:
                active["path"].unlink()
            except OSError as exc:
                raise RecordingError("cleanup_failed", f"Recording transfer succeeded but cleanup failed: {exc}") from exc
            self._active_recording = None
        finally:
            self._recording_lock.release()


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BioGlowBirdCast/1.0"

        def log_message(self, fmt, *args):
            return

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, data: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path == "/v1/recordings":
                try:
                    self._send(200, service.start_recording())
                except BusyError as exc:
                    self._send(409, error_payload("busy", str(exc)))
                except RecordingError as exc:
                    self._send(502, error_payload(exc.code, str(exc)))
                return
            if self.path != "/v1/snapshots":
                self._send(404, error_payload("not_found", "Unknown endpoint."))
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > 4096:
                    raise ValueError("invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {"region"} or not isinstance(body["region"], str):
                    raise ValueError('request must be {"region": "US-CA"}')
                region = body["region"].strip().upper()
                if region not in SUPPORTED_REGIONS:
                    self._send(400, error_payload("invalid_region", "Use a supported US BirdCast region such as US-CA."))
                    return
            except (ValueError, json.JSONDecodeError):
                self._send(400, error_payload("malformed_request", "Request body must be valid JSON with a US region."))
                return
            try:
                self._send(200, service.snapshot(region))
            except BusyError as exc:
                self._send(409, error_payload("busy", str(exc)))
            except LookupError:
                self._send(503, error_payload("birdcast_unavailable", f"BirdCast did not provide a current snapshot for {region}."))
            except Exception as exc:
                self._send(502, error_payload("scraper_failed", str(exc)))

        def do_GET(self):
            if self.path in ('/reading', '/health'):
                status, payload = service.light_sensor.response(health=self.path == '/health')
                self._send(status, payload)
                return
            if self.path.startswith("/v1/recordings/"):
                recording_id = self.path.rsplit("/", 1)[-1]
                transfer_claimed = False
                try:
                    metadata, data = service.recording(recording_id)
                    transfer_claimed = True
                    self._send_bytes(data, metadata["content_type"])
                except BusyError as exc:
                    self._send(409, error_payload("busy", str(exc)))
                except RecordingNotFound as exc:
                    self._send(404, error_payload(exc.code, str(exc)))
                except RecordingError as exc:
                    self._send(502, error_payload(exc.code, str(exc)))
                finally:
                    if transfer_claimed:
                        service.finish_transfer()
                return
            self._send(404, error_payload("not_found", "Unknown endpoint."))

        def do_DELETE(self):
            if not self.path.startswith("/v1/recordings/"):
                self._send(404, error_payload("not_found", "Unknown endpoint."))
                return
            recording_id = self.path.rsplit("/", 1)[-1]
            try:
                service.delete_recording(recording_id)
                self._send(200, {"status": "ok"})
            except BusyError as exc:
                self._send(409, error_payload("busy", str(exc)))
            except RecordingNotFound as exc:
                self._send(404, error_payload(exc.code, str(exc)))
            except RecordingError as exc:
                self._send(500, error_payload(exc.code, str(exc)))

    return Handler


def build_server(service: Service, host: str = "0.0.0.0", port: int = DEFAULT_PORT):
    return ThreadingHTTPServer((host, port), make_handler(service))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the BioGlow BirdCast LAN service.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    args = parser.parse_args(argv)
    service = Service(Path(args.root).resolve(), Path(args.root).resolve() / "config.json")
    server = build_server(service, port=args.port)
    print(f"BioGlow BirdCast service listening on port {args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
