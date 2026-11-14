#!/usr/bin/env python3
"""Small standard-library HTTP boundary for BirdCast and Pi microphone audio."""
from __future__ import annotations

import argparse
import copy
from contextlib import nullcontext
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from configuration_store import ConfigurationStore, validate_backup, validate_shared
import birdcast_scraper
from animal_analysis import AnalysisBusy, AnalysisCoordinator, AnalysisUnavailable
from animal_model import is_species_label
from animal_names import AnimalNames
from collection_schedule import CollectionSchedule
from light_sensor import LightSensor
from microphone_capture import pulse_environment, usb_capture_device
from lighting_control import LightingControl
from adaptive_lighting import AdaptiveConfig
from wled_serial import WLEDSerial
from presence import PresenceSensor
from reading_history import ReadingHistory
from telemetry import Telemetry
from recording_store import RecordingError, RecordingNotFound, RecordingStore

DEFAULT_PORT = 8000
DEFAULT_RECORDER = str(Path(__file__).with_name("microphone_capture.py"))
RECORDING_SECONDS = 5
SOUND_MAX_AGE_SECONDS = 120
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


def detect_system_services_status(
    bioglow_unit: str = "/etc/systemd/system/bioglow.service",
    cloudflared_unit: str = "/etc/systemd/system/cloudflared.service",
    cloudflared_token: str = "/etc/cloudflared/tunnel-token",
) -> dict[str, Any]:
    """Inspect systemd unit files and active states for BioGlow and Cloudflare."""
    bg_installed = os.path.isfile(bioglow_unit)
    bg_enabled = False
    bg_started = False
    if bg_installed:
        try:
            r_en = subprocess.run(
                ["systemctl", "is-enabled", "bioglow.service"],
                capture_output=True, text=True, timeout=1.5
            )
            bg_enabled = (r_en.stdout.strip() == "enabled")
        except (OSError, subprocess.SubprocessError):
            bg_enabled = False

        try:
            r_act = subprocess.run(
                ["systemctl", "is-active", "bioglow.service"],
                capture_output=True, text=True, timeout=1.5
            )
            bg_started = (r_act.stdout.strip() == "active")
        except (OSError, subprocess.SubprocessError):
            bg_started = False

    cf_bin = bool(shutil.which("cloudflared") or os.path.isfile("/usr/bin/cloudflared"))
    cf_unit_installed = os.path.isfile(cloudflared_unit)
    cf_installed = cf_bin or cf_unit_installed

    cf_token_configured = False
    if os.path.isfile(cloudflared_token):
        try:
            cf_token_configured = os.path.getsize(cloudflared_token) > 0
        except OSError:
            cf_token_configured = False

    cf_started = False
    if cf_unit_installed:
        try:
            r_cf = subprocess.run(
                ["systemctl", "is-active", "cloudflared.service"],
                capture_output=True, text=True, timeout=1.5
            )
            cf_started = (r_cf.stdout.strip() == "active")
        except (OSError, subprocess.SubprocessError):
            cf_started = False

    return {
        "bioglow_service": {
            "installed": bg_installed,
            "enabled": bg_enabled,
            "started": bg_started,
        },
        "cloudflare_tunnel": {
            "installed": cf_installed,
            "token_configured": cf_token_configured,
            "started": cf_started,
            "set_up": cf_installed and cf_token_configured,
        },
    }


class Service:
    SUPPORTED_MODES = ("normal", "direction_config", "manual_control", "user_aim")

    def __init__(self, root: Path, config: Path, runner=None, recorder: str = DEFAULT_RECORDER,
                 animal_analysis: bool = True):
        self.root = root.resolve()
        self.light_sensor = LightSensor()
        self.wled = WLEDSerial()
        self.lighting = LightingControl(background=True, transport=self.wled,
                                        adaptive_config=AdaptiveConfig.load(self.root))
        self.presence_sensor = PresenceSensor()
        self.telemetry = Telemetry(self.root)
        self.reading_history = ReadingHistory(self.root, on_entry=self.telemetry.enqueue)
        self.animal_names = AnimalNames(self.root)
        self.config = config
        self.aim_timer = None
        self.aim_deadline = None
        self.mode = "normal"
        self._services_status_cache: dict[str, Any] | None = None
        self._services_status_time: float = 0.0
        self._mode_lock = threading.RLock()
        self.settings = ConfigurationStore(self.root)
        self.direction_alignment = 144
        self.direction_beam_width = 1
        self.direction_offset = 0
        self._apply_preferences()
        self.runner = runner or self._run_scraper
        self._lock = threading.Lock()
        self.recorder = recorder
        self.recording_dir = self.root / "recordings"
        self.recording_store = RecordingStore(self.recording_dir)
        self.animal_analysis = AnalysisCoordinator(self.root, self.recording_store, enabled=animal_analysis,
                                                  on_result=self.log_recording)
        self._sound_checked_at = float("-inf")
        self._sound_decision = {"source": "unavailable", "reason": "No recording checked."}
        self.lighting.sound_reader = self.latest_sound
        self._recording_lock = threading.Lock()
        self._latest_lock = threading.Lock()
        self._cache_file = self.root / ".runtime" / "latest.json"
        self._latest: dict[str, Any] = {}
        self.collection_schedule = CollectionSchedule(self.presence_reading, self.start_recording,
                                                      collect_light=self.light_reading,
                                                      cleanup_recordings=self.expire_recordings)
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
        if source == "birdcast":
            self.lighting.update_migration(result if status == 200 else None)
        if source == "microphone":
            self._sound_checked_at = float("-inf")
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

    def _apply_preferences(self):
        prefs = self.settings.shared
        self.direction_alignment = prefs["alignment"]
        self.direction_beam_width = prefs["beam_width"]
        self.direction_offset = prefs["offset"]
        self.lighting.load_preferences(prefs)

    def configure_lighting(self, **body):
        with self._mode_lock:
            mode = body.get("mode")
            if mode is not None and mode not in ("on", "off", "auto", "distance_test", "direction_config"):
                raise ValueError("Invalid lighting mode.")
            if mode == "direction_config" and self.mode != "direction_config":
                raise ValueError("Direction Config station mode must be active.")
            if self.lighting.status()["mode"] == "distance_test" and mode is None:
                raise ValueError("Select On, Off, or Auto before changing appearance.")
            calibrating = mode == "direction_config" and self.mode == "direction_config"
            if not calibrating and self.mode != "manual_control" and (mode in ("on", "off") or "brightness" in body or "color" in body):
                raise ValueError("Manual control mode must be active to change appearance.")
            prefs = dict(self.settings.shared)
            if "brightness" in body:
                prefs["calibration_brightness" if calibrating else "manual_brightness"] = body["brightness"]
            if "color" in body:
                prefs["color"] = body["color"]
            prefs = validate_shared(prefs)
            if prefs != self.settings.shared:
                self.settings.save(prefs)
                self._apply_preferences()
            return self.lighting.configure(**body)

    def set_station_mode(self, mode: str) -> str:
        if mode not in self.SUPPORTED_MODES:
            raise ValueError(f"Unknown mode: {mode}. Choose from: {', '.join(self.SUPPORTED_MODES)}.")
        with self._mode_lock:
            if getattr(self, "aim_timer", None):
                self.aim_timer.cancel()
            self.aim_deadline = None
            self.mode = mode
            if mode in ("direction_config", "user_aim"):
                try:
                    self.lighting.set_direction_alignment(self.direction_alignment, beam_width=self.direction_beam_width, offset=self.direction_offset, brightness=self.settings.shared["calibration_brightness"])
                except Exception as exc:
                    logging.getLogger(__name__).warning("Could not set direction alignment: %s", exc)
            elif mode == "normal":
                try:
                    self.lighting.set_mode("auto")
                except Exception as exc:
                    logging.getLogger(__name__).warning("Could not restore auto lighting: %s", exc)
            return self.mode

    def user_aim(self, body):
        import time
        with self._mode_lock:
            action = body.get("action")
            if action == "stop":
                self.set_station_mode("normal")
            elif action in ("start", "extend", "direction"):
                if action == "start":
                    if body.get("expected_mode") != self.mode:
                        raise ValueError("Station mode changed. Review and try again.")
                    self.set_station_mode("user_aim")
                if self.mode != "user_aim":
                    raise ValueError("Start aiming first.")
                if action == "direction":
                    angle = body.get("angle")
                    if type(angle) not in (int, float) or not 0 <= angle < 360:
                        raise ValueError("Angle must be between 0 and 360.")
                    self.set_direction_alignment(self.direction_alignment, offset=round(angle / 360 * self.direction_alignment) % self.direction_alignment)
                if getattr(self, "aim_timer", None):
                    self.aim_timer.cancel()
                self.aim_deadline = time.time() + 300
                deadline = self.aim_deadline
                def expire():
                    with self._mode_lock:
                        if self.mode == "user_aim" and self.aim_deadline == deadline:
                            self.set_station_mode("normal")
                self.aim_timer = threading.Timer(300, expire)
                self.aim_timer.daemon = True
                self.aim_timer.start()
            else:
                raise ValueError("Unknown aiming action.")
            return self.latest()

    def set_direction_alignment(self, spacing, beam_width=None, offset=None):
        with self._mode_lock:
            return self._set_direction_alignment(spacing, beam_width, offset)

    def _set_direction_alignment(self, spacing: int, beam_width: int = None, offset: int = None) -> dict[str, Any]:
        if not (type(spacing) is int and 1 <= spacing <= 144):
            raise ValueError("alignment must be an integer from 1 to 144")
        max_w = min(143, 2 * (spacing // 2) + 1)
        if beam_width is None:
            beam_width = min(self.direction_beam_width, max_w)
        elif not (type(beam_width) is int and 1 <= beam_width <= max_w and beam_width % 2 == 1):
            raise ValueError(f"beam_width must be an odd integer from 1 to {max_w}")
        if offset is None:
            offset = self.direction_offset % spacing
        elif not (type(offset) is int and 0 <= offset < spacing):
            raise ValueError(f"offset must be an integer from 0 to {spacing - 1}")
        self.settings.save({**self.settings.shared, "alignment": spacing,
                            "beam_width": beam_width, "offset": offset})
        self._apply_preferences()
        return self.lighting.set_direction_alignment(spacing, beam_width=beam_width, offset=offset,
                                                     brightness=self.settings.shared["calibration_brightness"])

    def get_services_status(self, force_refresh: bool = False) -> dict[str, Any]:
        import time as _time
        now = _time.monotonic()
        if not force_refresh and self._services_status_cache is not None and (now - self._services_status_time < 5.0):
            return copy.deepcopy(self._services_status_cache)
        status = detect_system_services_status()
        self._services_status_cache = status
        self._services_status_time = now
        return copy.deepcopy(status)

    def latest(self) -> dict[str, Any]:
        with self._mode_lock:
            return self._latest_snapshot()

    def lamp_telemetry(self):
        with self._mode_lock:
            lighting = self.lighting.status()
            lighting.pop("radar_snapshot", None)
            return {"mode": self.mode, "lighting": lighting,
                    "directionality": {"alignment": self.direction_alignment,
                                       "beam_width": self.direction_beam_width,
                                       "offset": self.direction_offset,
                                       "angle_degrees": round(self.direction_offset / self.direction_alignment * 360, 1)}}

    def _latest_snapshot(self) -> dict[str, Any]:
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
                "configuration": self.settings.snapshot(),
                "collection": self.collection_schedule.status(),
                "aim_deadline": getattr(self, "aim_deadline", None),
                "mode": self.mode, "modes": list(self.SUPPORTED_MODES),
                "directionality": {
                    "alignment": self.direction_alignment,
                    "beam_width": self.direction_beam_width,
                    "offset": self.direction_offset,
                    "max_beam_width": min(143, 2 * (self.direction_alignment // 2) + 1),
                    "angle_degrees": round(self.direction_offset / self.direction_alignment * 360, 1),
                },
                "services": self.get_services_status(),
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

    def latest_sound(self):
        """Poll the newest retained recording without capturing or blocking for inference."""
        if getattr(self, "recorder", None) == DEFAULT_RECORDER:
            current = time.monotonic()
            if current - getattr(self, "_microphone_checked_at", float("-inf")) >= 10:
                try:
                    if not usb_capture_device():
                        pulse_environment(os.environ)
                    self._microphone_error = None
                except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
                    self._microphone_error = str(exc)
                self._microphone_checked_at = current
                self._sound_checked_at = float("-inf")
            if self._microphone_error:
                return {"source": "unavailable", "reason": self._microphone_error + " Using brightness rules."}
        microphone = getattr(self, "_latest", {}).get("microphone", {})
        if microphone.get("http_status", 200) != 200:
            return {"source": "unavailable", "reason": "Latest microphone recording failed; using brightness rules."}
        current = time.monotonic()
        if current - self._sound_checked_at < 2:
            return dict(self._sound_decision)
        self._sound_checked_at = current
        decision = {"source": "unavailable", "reason": "No recordings available; using brightness rules."}
        try:
            try:
                stamp = self.recording_store.directory.stat().st_mtime_ns
            except FileNotFoundError:
                stamp = None
            if not hasattr(self, "_sound_recordings_stamp") or stamp != self._sound_recordings_stamp:
                self._sound_recordings = self.recording_store.list_recordings()
                self._sound_recordings_stamp = stamp
            recordings = self._sound_recordings
            if recordings:
                metadata = recordings[0]
                try:
                    timestamp = metadata.get("created_at")
                    if not isinstance(timestamp, str):
                        raise ValueError("Missing capture timestamp")
                    captured_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - captured_at).total_seconds()
                except (KeyError, TypeError, ValueError):
                    age = None
                if age is None or not 0 <= age <= SOUND_MAX_AGE_SECONDS:
                    decision = {"source": "unavailable", "recording_id": metadata["recording_id"],
                                "age_seconds": age,
                                "reason": "Latest audio is stale or has no valid capture time; using brightness rules."}
                    self._sound_decision = decision
                    return dict(decision)
                analysis = self.animal_analysis.status(metadata)
                if analysis.get("state") not in ("failed", "unavailable"):
                    analysis = self.animal_analysis.request(metadata, sound=True)
                decision = {"source": "unavailable" if analysis["state"] in ("failed", "unavailable") else analysis.get("sound", {}).get("source", "unknown"),
                            "recording_id": metadata["recording_id"],
                            "analysis_state": analysis["state"]}
        except Exception as exc:
            decision = {"source": "unavailable", "reason": str(exc)}
        self._sound_decision = decision
        return dict(decision)

    def recordings(self) -> list[dict[str, Any]]:
        recordings = self.recording_store.list_recordings()
        for metadata in recordings:
            result = self.animal_analysis.status(metadata)
            metadata["analysis"] = {key: result[key] for key in
                                    ("state", "error", "outdated", "candidates", "processing_seconds", "sound")
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

    def expire_recordings(self, now=None):
        """Remove recordings older than five days using the normal safe deletion path."""
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=5)
        deleted = 0
        for metadata in self.recording_store.list_recordings():
            try:
                created = datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00"))
                if created.tzinfo is None:
                    raise ValueError("Recording timestamp has no timezone")
                if created >= cutoff:
                    continue
                self.delete_recording(metadata["recording_id"])
                deleted += 1
            except BusyError:
                # Retry on the next sweep after capture or inference finishes.
                continue
            except (OSError, ValueError, RecordingError):
                logging.getLogger(__name__).exception("Could not expire recording %s", metadata["recording_id"])
        return deleted

    def delete_recording(self, recording_id: str) -> None:
        self.recording_store.path(recording_id)  # Validate before accessing analysis paths.
        if not self._recording_lock.acquire(blocking=False):
            raise BusyError("A recording is being captured. Try deleting when capture finishes.")
        try:
            def remove():
                self.recording_store.delete(recording_id)
                self.reading_history.delete_recording(recording_id)
            self.animal_analysis.delete(recording_id, remove)
            self._sound_checked_at = float("-inf")
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
        timeout = 10  # Bound stalled request headers and bodies on the LAN.

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
            try:
                raw = self.rfile.read(length)
            except TimeoutError as exc:
                raise ValueError("Timed out reading request body.") from exc
            if len(raw) != length:
                raise ValueError("Incomplete request body.")
            body = json.loads(raw) if length else {}
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            return body

        def do_POST(self):
            settings_request = self.path in (
                "/v1/direction", "/v1/direction/alignment", "/v1/directionality/alignment",
                "/v1/lighting", "/v1/station-mode", "/v1/mode",
                "/v1/configuration/preview", "/v1/configuration/restore")
            with service._mode_lock if settings_request else nullcontext():
                try:
                    self._post()
                except OSError as exc:
                    if not settings_request:
                        raise
                    self._send(500, error_payload("configuration_not_saved", f"Change not saved: {exc}"))

        def _post(self):
            if self.path == "/v1/user-aim":
                try:
                    self._send(200, service.user_aim(self._body()))
                except ValueError as exc:
                    self._send(409, error_payload("aim_conflict", str(exc)))
                except Exception as exc:
                    self._send(502, error_payload("aim_failed", str(exc)))
                return

            if self.path in ("/v1/configuration/preview", "/v1/configuration/restore"):
                try:
                    body = self._body()
                    required = {"backup"} if self.path.endswith("preview") else {"backup", "revision"}
                    if set(body) != required:
                        raise ValueError("Invalid restore request fields.")
                    backup = validate_backup(body["backup"], SUPPORTED_REGIONS)
                    if self.path.endswith("preview"):
                        self._send(200, {"current": service.settings.snapshot(), "proposed": backup})
                        return
                    if type(body["revision"]) is not str or body["revision"] != service.settings.revision:
                        self._send(409, error_payload("configuration_changed", "Settings changed. Request a new preview."))
                        return
                    service.settings.save(backup["shared"], replace_invalid=True)
                    service._apply_preferences()
                    if service.mode == "direction_config":
                        lighting = service.lighting.set_direction_alignment(
                            service.direction_alignment, service.direction_beam_width, service.direction_offset,
                            brightness=service.settings.shared["calibration_brightness"])
                    elif service.mode == "manual_control":
                        lighting = service.lighting.configure(mode=service.lighting.status()["mode"])
                    else:
                        lighting = service.lighting.status()
                    self._send(200, {"saved": True, "configuration": service.settings.snapshot(),
                                     "browser": backup["browser"], "lighting": lighting})
                except (ValueError, TypeError) as exc:
                    self._send(400, error_payload("invalid_backup", str(exc)))
                return
            if self.path in ("/v1/direction", "/v1/direction/alignment", "/v1/directionality/alignment"):
                try:
                    body = self._body()
                    if not isinstance(body, dict) or set(body) - {"alignment", "beam_width", "offset", "angle", "angle_degrees"}:
                        raise ValueError("Unknown direction fields.")
                    if "alignment" not in body and "beam_width" not in body and "offset" not in body and "angle" not in body and "angle_degrees" not in body:
                        raise ValueError("Request body must include 'alignment', 'beam_width', 'offset', or 'angle'.")
                    spacing = body.get("alignment", service.direction_alignment)
                    if not (isinstance(spacing, int) and 1 <= spacing <= 144):
                        raise ValueError("Alignment must be an integer between 1 and 144.")
                    max_w = min(143, 2 * (spacing // 2) + 1)
                    bw = body.get("beam_width")
                    if bw is not None:
                        if not (isinstance(bw, int) and 1 <= bw <= max_w and bw % 2 == 1):
                            raise ValueError(f"beam_width must be an odd integer between 1 and {max_w}.")
                    else:
                        bw = min(service.direction_beam_width, max_w)
                    offset = body.get("offset")
                    if offset is None:
                        angle = body.get("angle") if "angle" in body else body.get("angle_degrees")
                        if angle is not None:
                            if not isinstance(angle, (int, float)):
                                raise ValueError("angle must be a number.")
                            offset = round(((float(angle) % 360) / 360) * spacing) % spacing
                        else:
                            offset = service.direction_offset % spacing
                    elif not (isinstance(offset, int) and 0 <= offset < spacing):
                        raise ValueError(f"offset must be an integer between 0 and {spacing - 1}.")
                    if service.mode != "direction_config":
                        self._send(409, error_payload("invalid_mode", "Direction config mode must be active to adjust direction parameters."))
                        return
                    lighting = service.set_direction_alignment(spacing, beam_width=bw, offset=offset)
                    self._send(200, {
                        "status": "ok",
                        "alignment": service.direction_alignment,
                        "beam_width": service.direction_beam_width,
                        "offset": service.direction_offset,
                        "max_beam_width": max_w,
                        "angle_degrees": round(service.direction_offset / service.direction_alignment * 360, 1),
                        "lighting": lighting
                    })
                except ValueError as exc:
                    self._send(400, error_payload("malformed_request", str(exc)))
                return
            if self.path == "/v1/lighting":
                try:
                    body = self._body()
                    if not body or set(body) - {"mode", "brightness", "color", "alignment", "beam_width", "offset", "angle", "angle_degrees"} or any(value is None for value in body.values()):
                        raise ValueError('Provide mode (on/off/auto/distance_test/direction_config), brightness (1–255), color (#RRGGBB), alignment (1–144), beam_width, or offset.')
                    if any(key in body for key in ("alignment", "beam_width", "offset", "angle", "angle_degrees")) and any(key in body for key in ("mode", "brightness", "color")):
                        raise ValueError("Change direction and appearance separately.")
                    if "alignment" in body or "beam_width" in body or "offset" in body or "angle" in body or "angle_degrees" in body:
                        spacing = body.get("alignment", service.direction_alignment)
                        if not (isinstance(spacing, int) and 1 <= spacing <= 144):
                            raise ValueError("alignment must be an integer from 1 to 144")
                        max_w = min(143, 2 * (spacing // 2) + 1)
                        bw = body.get("beam_width")
                        if bw is not None:
                            if not (isinstance(bw, int) and 1 <= bw <= max_w and bw % 2 == 1):
                                raise ValueError(f"beam_width must be an odd integer from 1 to {max_w}")
                        else:
                            bw = min(service.direction_beam_width, max_w)
                        offset = body.get("offset")
                        if offset is None:
                            angle = body.get("angle") if "angle" in body else body.get("angle_degrees")
                            if angle is not None:
                                if not isinstance(angle, (int, float)):
                                    raise ValueError("angle must be a number.")
                                offset = round(((float(angle) % 360) / 360) * spacing) % spacing
                            else:
                                offset = service.direction_offset % spacing
                        elif not (isinstance(offset, int) and 0 <= offset < spacing):
                            raise ValueError(f"offset must be an integer between 0 and {spacing - 1}.")
                        if service.mode != "direction_config":
                            self._send(409, error_payload("invalid_mode", "Direction config mode must be active to adjust direction parameters."))
                            return
                        lighting = service.set_direction_alignment(spacing, beam_width=bw, offset=offset)
                    elif set(body) == {"mode"}:
                        lighting = service.configure_lighting(**body)
                    else:
                        lighting = service.configure_lighting(**body)
                    self._send(200, {"status": "ok", "saved": any(key != "mode" for key in body),
                                     "hardware_delivery": lighting["status"], "lighting": lighting})
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
            if self.path in ("/v1/station-mode", "/v1/mode"):
                try:
                    body = self._body()
                    if not isinstance(body, dict) or set(body) != {"mode"} or not isinstance(body["mode"], str):
                        raise ValueError(f"Expected JSON object with 'mode'. Choose from: {', '.join(service.SUPPORTED_MODES)}.")
                    mode = service.set_station_mode(body["mode"])
                    self._send(200, {"status": "ok", "mode": mode, "modes": list(service.SUPPORTED_MODES)})
                except ValueError as exc:
                    self._send(400, error_payload("malformed_request", str(exc)))
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
            if path == "/v1/configuration":
                with service._mode_lock:
                    self._send(200, service.settings.snapshot())
                return
            assets = {
                "/user": ("user.html", "text/html; charset=utf-8"),
                "/dev": ("index.html", "text/html; charset=utf-8"),
                "/user.js": ("user.js", "text/javascript; charset=utf-8"),
                "/user.css": ("user.css", "text/css; charset=utf-8"),
                "/": ("choose.html", "text/html; charset=utf-8"),
                "/distance-test": ("index.html", "text/html; charset=utf-8"),
                "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
                "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
                "/favicon.png": ("favicon.png", "image/png"),
                "/glow-favicon.png": ("favicon.png", "image/png"),
                "/favicon.ico": ("favicon.png", "image/png"),
            }
            if path in assets:
                filename, content_type = assets[path]
                asset = Path(__file__).resolve().parent / "web" / filename
                if asset.is_symlink() or not asset.is_file():
                    self._send(404, error_payload("not_found", "Asset not found."))
                else:
                    self._send_bytes(asset.read_bytes(), content_type)
                return
            if path == "/v1/wled-log":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                values = query.get("after", ["0"])
                if (set(query) - {"after"} or len(values) != 1
                        or not values[0].isascii() or not values[0].isdigit()
                        or len(values[0]) > 18):
                    self._send(400, error_payload("invalid_cursor", "Use a nonnegative integer after cursor."))
                    return
                self._send(200, service.wled.log.snapshot(int(values[0])))
                return
            if path in ("/v1/station-mode", "/v1/mode"):
                self._send(200, {"status": "ok", "mode": service.mode, "modes": list(service.SUPPORTED_MODES)})
                return
            if path in ("/v1/direction", "/v1/direction/alignment", "/v1/directionality/alignment"):
                self._send(200, {
                    "status": "ok",
                    "alignment": service.direction_alignment,
                    "beam_width": service.direction_beam_width,
                    "offset": service.direction_offset,
                    "max_beam_width": min(143, 2 * (service.direction_alignment // 2) + 1),
                    "angle_degrees": round(service.direction_offset / service.direction_alignment * 360, 1),
                    "mode": service.mode
                })
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
    service.telemetry.start(service.lamp_telemetry)
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
        service.telemetry.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
