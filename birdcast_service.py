#!/usr/bin/env python3
"""Small standard-library HTTP boundary for BirdCast and Pi microphone audio."""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import birdcast_scraper
from animal_analysis import AnalysisBusy, AnalysisCoordinator, AnalysisUnavailable
from animal_model import is_species_label
from animal_names import AnimalNames
from collection_schedule import CollectionSchedule
from light_sensor import LightSensor
from lighting_control import LightingControl
from wled_serial import WLEDSerial
from presence import PresenceSensor
from reading_history import ReadingHistory
from recording_store import RecordingError, RecordingNotFound, RecordingStore

DEFAULT_PORT = 8000
DEFAULT_RECORDER = str(Path(__file__).with_name("microphone_capture.py"))
RECORDING_SECONDS = 5
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


class Service:
    def __init__(self, root: Path, config: Path, runner=None, recorder: str = DEFAULT_RECORDER,
                 animal_analysis: bool = True):
        self.root = root.resolve()
        self.light_sensor = LightSensor()
        self.lighting = LightingControl(background=True, transport=WLEDSerial())
        self.presence_sensor = PresenceSensor()
        self.reading_history = ReadingHistory(self.root)
        self.animal_names = AnimalNames(self.root)
        self.config = config
        self.runner = runner or self._run_scraper
        self._lock = threading.Lock()
        self.recorder = recorder
        self.recording_dir = self.root / "recordings"
        self.recording_store = RecordingStore(self.recording_dir)
        self.animal_analysis = AnalysisCoordinator(self.root, self.recording_store, enabled=animal_analysis,
                                                  on_result=self.log_recording)
        self._recording_lock = threading.Lock()
        self._latest_lock = threading.Lock()
        self._cache_file = self.root / ".runtime" / "latest.json"
        self._latest: dict[str, Any] = {}
        self.collection_schedule = CollectionSchedule(self.collect_sensors, self.start_recording)
        try:
            cached = json.loads(self._cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict):
                self._latest = {key: value for key, value in cached.items()
                                if key in ("light", "birdcast", "microphone") and isinstance(value, dict)
                                and isinstance(value.get("result"), dict)
                                and isinstance(value.get("http_status"), int)
                                and (value.get("last_success") is None
                                     or isinstance(value.get("last_success"), dict))}
        except (OSError, ValueError):
            pass

    def remember(self, source: str, status: int, result: dict[str, Any]) -> None:
        with self._latest_lock:
            previous = self._latest.get(source, {})
            self._latest[source] = {
                "requested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "http_status": status, "result": copy.deepcopy(result),
                "last_success": copy.deepcopy(result if status == 200 else previous.get("last_success")),
            }
            try:
                birdcast_scraper.atomic_write_json(self._cache_file, self._latest)
            except OSError as exc:
                # A cache failure must not turn a completed sensor read or capture into a failure.
                logging.getLogger(__name__).warning("Latest results remain in memory; cache write failed: %s", exc)

    def latest(self) -> dict[str, Any]:
        with self._latest_lock:
            result = copy.deepcopy(self._latest)
        # Radar is passive: this reads worker state without opening UART or sending commands.
        lighting = self.lighting.status()
        sampled_radar = lighting.pop("radar_snapshot", None)
        if isinstance(sampled_radar, dict):
            result["radar"] = sampled_radar
        else:
            status, radar = self.presence_sensor.response()
            result["radar"] = {"http_status": status, "result": radar}
        return {"status": "ok", "sources": result, "lighting": lighting,
                "collection": self.collection_schedule.status(),
                "regions": [{"code": f"US-{code}", "name": name}
                            for code, name in birdcast_scraper.STATE_NAMES.items()]}

    def log_reading(self, source: str, status: int, result: dict[str, Any]) -> None:
        try:
            self.reading_history.append(source, status, result)
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning("Could not save %s reading: %s", source, exc)
            result["history_error"] = "This reading could not be saved to its text log."

    def collect_sensors(self):
        for read in (self.presence_reading, self.light_reading):
            try:
                read()
            except Exception:
                logging.getLogger(__name__).exception("Scheduled sensor read failed")

    def presence_reading(self, health: bool = False) -> tuple[int, dict[str, Any]]:
        status, result = self.presence_sensor.response(health=health)
        self.log_reading("radar", status, result)
        return status, result

    def light_reading(self, health: bool = False) -> tuple[int, dict[str, Any]]:
        status, result = self.light_sensor.response(health=health)
        self.lighting.update(result.get("lux") if status == 200 else None)
        self.log_reading("light", status, result)
        # Keep the last measurement available even when callers request health only.
        if not health or status != 200:
            self.remember("light", status, result)
        return status, result

    def snapshot(self, region: str) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise BusyError("Another BirdCast snapshot is already in progress.")
        try:
            result = self.runner(region)
            self.log_reading("birdcast", 200, result)
            self.remember("birdcast", 200, result)
            return result
        except Exception as exc:
            status = 503 if isinstance(exc, LookupError) else 502
            code = "birdcast_unavailable" if status == 503 else "scraper_failed"
            self.log_reading("birdcast", status, {**error_payload(code, str(exc)), "region": {"code": region}})
            self.remember("birdcast", status, error_payload(code, str(exc)))
            raise
        finally:
            self._lock.release()

    def _run_scraper(self, region: str) -> dict[str, Any]:
        runtime = self.root / ".runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        output = runtime / "birdcast_snapshot.json"
        config = runtime / "birdcast_config.json"
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
            raise BusyError("Another microphone recording is already in progress.")
        try:
            self.recording_dir.mkdir(parents=True, exist_ok=True)
            recording_id = uuid.uuid4().hex
            expected = self.recording_store.path(recording_id)
            try:
                with self.animal_analysis.capture():
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
            metadata, _ = self.recording_store.read(recording_id)
            if metadata["duration_seconds"] != RECORDING_SECONDS:
                raise RecordingError("invalid_audio", "New microphone captures must contain exactly five seconds.")
            self.recording_store.publish_metadata(metadata)
            metadata["analysis"] = self.animal_analysis.automatic(metadata)
            self.log_recording(metadata, metadata["analysis"])
            self.remember("microphone", 200, metadata)
            return metadata
        except RecordingError as exc:
            self.remember("microphone", 502, error_payload(exc.code, str(exc)))
            raise
        except OSError as exc:
            error = RecordingError("recorder_failed", str(exc))
            self.remember("microphone", 502, error_payload(error.code, str(error)))
            raise error from exc
        finally:
            self._recording_lock.release()

    def recording(self, recording_id: str) -> tuple[dict[str, Any], bytes]:
        return self.recording_store.read(recording_id)

    def recordings(self) -> list[dict[str, Any]]:
        recordings = self.recording_store.list_recordings()
        for metadata in recordings:
            result = self.animal_analysis.status(metadata)
            metadata["analysis"] = {key: result[key] for key in
                                    ("state", "error", "outdated", "candidates", "processing_seconds")
                                    if key in result}
        return recordings

    def log_recording(self, metadata, analysis):
        try:
            self.reading_history.recording(metadata, analysis)
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning("Could not log recording analysis: %s", exc)

    def history(self, source, window, region=None):
        return {"status": "ok", **self.reading_history.series(source, window, region)}

    def analyze_recording(self, recording_id: str, request: bool = False) -> dict[str, Any]:
        metadata, _ = self.recording_store.read(recording_id)
        analysis = (self.animal_analysis.request(metadata) if request
                    else self.animal_analysis.status(metadata))
        return {"status": "ok", "recording_id": recording_id, "analysis": analysis}

    def finish_transfer(self) -> None:
        # Retained for callers using the original transfer lifecycle.
        pass

    def delete_recording(self, recording_id: str) -> None:
        self.recording_store.path(recording_id)  # Validate before accessing analysis paths.
        if not self._recording_lock.acquire(blocking=False):
            raise BusyError("A recording is being captured. Try deleting when capture finishes.")
        try:
            def remove():
                self.recording_store.delete(recording_id)
                self.reading_history.delete_recording(recording_id)
            self.animal_analysis.delete(recording_id, remove)
            with self._latest_lock:
                entry = self._latest.get("microphone", {})
                if any((entry.get(key) or {}).get("recording_id") == recording_id for key in ("result", "last_success")):
                    self._latest.pop("microphone", None)
                    try:
                        birdcast_scraper.atomic_write_json(self._cache_file, self._latest)
                    except OSError as exc:
                        logging.getLogger(__name__).warning("Could not update recording cache after deletion: %s", exc)
        except AnalysisBusy as exc:
            raise BusyError(str(exc)) from exc
        except OSError as exc:
            raise RecordingError("delete_failed", "Recording cleanup could not be completed.") from exc
        finally:
            self._recording_lock.release()


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BioGlowBirdCast/1.0"

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                # Navigation or a cancelled fetch can close the client socket
                # during headers, body writes, or the final response flush.
                self.close_connection = True

        def log_message(self, fmt, *args):
            return

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, data: bytes, content_type: str, metadata=None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if metadata:
                self.send_header("X-Content-SHA256", metadata["sha256"])
                self.send_header("Content-Disposition", f'inline; filename="{metadata["recording_id"]}.wav"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self, optional: bool = False) -> dict[str, Any]:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Use Content-Length.")
            length = int(self.headers.get("Content-Length", "0" if optional else "-1"))
            if not 0 <= length <= 4096:
                raise ValueError("Invalid request size.")
            body = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            return body

        def do_POST(self):
            if self.path == "/v1/lighting":
                try:
                    body = self._body()
                    if not body or set(body) - {"mode", "brightness", "color"} or any(value is None for value in body.values()):
                        raise ValueError('Provide mode (on/off/auto/distance_test), brightness (1–255), or color (#RRGGBB).')
                    if set(body) == {"mode"}:
                        lighting = service.lighting.set_mode(body["mode"])
                    else:
                        lighting = service.lighting.configure(**body)
                    self._send(503 if lighting["status"] == "controller_unavailable" else 200,
                               {"status": "ok" if lighting["status"] != "controller_unavailable" else "error",
                                "lighting": lighting})
                except ValueError as exc:
                    self._send(400, error_payload("malformed_request", str(exc)))
                return
            if self.path == "/v1/recording-schedule":
                try:
                    body = self._body()
                    if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
                        raise ValueError("Expected enabled boolean.")
                    self._send(200, {"status": "ok", "collection": service.collection_schedule.set_recording(body["enabled"])})
                except ValueError:
                    self._send(400, error_payload("malformed_request", "Use {\"enabled\": true} or {\"enabled\": false}."))
                return
            if self.path.startswith("/v1/recordings/") and self.path.endswith("/analysis"):
                try:
                    if self._body(optional=True):
                        raise ValueError("Analysis request must be empty or {}.")
                    identifier = self.path[len("/v1/recordings/"):-len("/analysis")]
                    result = service.analyze_recording(identifier, request=True)
                    self._send(200 if result["analysis"]["state"] == "completed" else 202, result)
                except ValueError as exc:
                    self._send(400, error_payload("malformed_request", str(exc)))
                except RecordingNotFound as exc:
                    self._send(404, error_payload(exc.code, str(exc)))
                except RecordingError as exc:
                    self._send(502, error_payload(exc.code, str(exc)))
                except (AnalysisUnavailable, OSError) as exc:
                    self._send(503, error_payload("analysis_unavailable", str(exc)))
                return
            if self.path == "/v1/recordings":
                try:
                    if self._body(optional=True):
                        raise ValueError("Recording request must be empty or {}.")
                except ValueError:
                    self._send(400, error_payload("malformed_request", "Recording request must be empty or {}."))
                    return
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
                body = self._body()
                if set(body) != {"region"} or not isinstance(body["region"], str):
                    raise ValueError('request must be {"region": "US-CA"}')
                region = body["region"].strip().upper()
                if region not in SUPPORTED_REGIONS:
                    self._send(400, error_payload("invalid_region", "Use a supported US BirdCast region such as US-CA."))
                    return
            except ValueError:
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
            path = urlsplit(self.path).path
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/distance-test": ("index.html", "text/html; charset=utf-8"),
                "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
                "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
            }
            if path in assets:
                filename, content_type = assets[path]
                asset = Path(__file__).resolve().parent / "web" / filename
                if asset.is_symlink() or not asset.is_file():
                    self._send(404, error_payload("not_found", "Asset not found."))
                else:
                    self._send_bytes(asset.read_bytes(), content_type)
                return
            if path == "/v1/latest":
                self._send(200, service.latest())
                return
            if path == "/v1/history":
                query = parse_qs(urlsplit(self.path).query)
                source, window = query.get("source", [""])[0], query.get("range", ["1h"])[0]
                region = query.get("region", [None])[0]
                if (source not in ReadingHistory.FILES or window not in ReadingHistory.RANGES
                        or (region is not None and region not in SUPPORTED_REGIONS)):
                    self._send(400, error_payload("invalid_history", "Choose a valid source, time range, and region."))
                    return
                try:
                    self._send(200, service.history(source, window, region))
                except OSError as exc:
                    self._send(503, error_payload("history_unavailable", str(exc)))
                return
            if path == "/presence":
                self._send(*service.presence_reading())
                return
            if path == "/v1/animal-names":
                names = parse_qs(urlsplit(self.path).query).get("name", [])
                if not names or len(names) > 30 or any(not is_species_label(name) or len(name) > 150 for name in names):
                    self._send(400, error_payload("invalid_names", "Provide up to 30 scientific species names."))
                    return
                self._send(200, {"status": "ok", "names": service.animal_names.resolve(names)})
                return
            if path in ("/reading", "/health"):
                status, payload = service.light_reading(health=path == "/health")
                if path == "/health":
                    radar_status, radar = service.presence_reading(health=True)
                    payload = {**payload, "presence_sensor": radar,
                               "serial_connected": radar["serial_connected"], "fresh": radar["fresh"]}
                    if radar_status != 200:
                        payload["status"] = "error"
                    status = max(status, radar_status)
                self._send(status, payload)
                return
            if path.startswith("/v1/recordings/") and path.endswith("/analysis"):
                try:
                    identifier = path[len("/v1/recordings/"):-len("/analysis")]
                    result = service.analyze_recording(identifier)
                    unavailable = result["analysis"]["state"] == "unavailable"
                    if unavailable:
                        result.update(error_payload("analysis_unavailable", result["analysis"]["error"]))
                    self._send(503 if unavailable else 200, result)
                except RecordingNotFound as exc:
                    self._send(404, error_payload(exc.code, str(exc)))
                except RecordingError as exc:
                    self._send(502, error_payload(exc.code, str(exc)))
                return
            if path == "/v1/recordings" or path.startswith("/v1/recordings/"):
                try:
                    if path == "/v1/recordings":
                        self._send(200, {"status": "ok", "recordings": service.recordings()})
                    else:
                        metadata, data = service.recording(path[len("/v1/recordings/"):])
                        self._send_bytes(data, metadata["content_type"], metadata)
                except RecordingNotFound as exc:
                    self._send(404, error_payload(exc.code, str(exc)))
                except RecordingError as exc:
                    self._send(502, error_payload(exc.code, str(exc)))
                return
            self._send(404, error_payload("not_found", "Unknown endpoint."))

        def do_DELETE(self):
            path = urlsplit(self.path).path
            if not path.startswith("/v1/recordings/"):
                self._send(404, error_payload("not_found", "Unknown endpoint."))
                return
            recording_id = path[len("/v1/recordings/"):]
            try:
                service.delete_recording(recording_id)
                self._send(200, {"status": "ok", "recording_id": recording_id,
                                 "retained": False, "deleted": True})
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
    parser.add_argument("--no-animal-analysis", action="store_true",
                        help="Disable Perch identification; preserve playback and saved results.")
    args = parser.parse_args(argv)
    service = Service(Path(args.root).resolve(), Path(args.root).resolve() / "config.json",
                      animal_analysis=not args.no_animal_analysis)
    server = build_server(service, port=args.port)
    service.presence_sensor.start()
    service.lighting.start_distance_sampling(service.presence_sensor.response)
    service.collection_schedule.start()
    try:
        service.animal_analysis.start()
    except OSError as exc:
        logging.getLogger(__name__).warning("Animal queue could not resume: %s", exc)
    print(f"BioGlow dashboard listening on http://0.0.0.0:{args.port}/ (use the Pi LAN address)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        service.collection_schedule.stop()
        service.lighting.stop()
        service.animal_analysis.stop()
        service.presence_sensor.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
