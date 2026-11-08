"""Durable analysis queue and subprocess lifecycle, without importing ML libraries."""
from __future__ import annotations

import array
import math
import copy
import importlib.util
import json
import logging
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from animal_model import IDENTITY, check_assets
from birdcast_scraper import atomic_write_json

LOG = logging.getLogger(__name__)
JOB_NAME = re.compile(r"[0-9a-f]{32}\.json")


# Maximum DC-corrected 100 ms RMS, relative to full-scale signed PCM.
QUIET_THRESHOLD_DBFS = -50.0


def audio_level(data: bytes) -> dict:
    """Measure validated 48 kHz PCM without loading the inference dependencies."""
    samples = array.array("h")
    samples.frombytes(data[44:])
    if sys.byteorder != "little":
        samples.byteswap()
    maximum = 0.0
    for start in range(0, len(samples), 4800):
        block = samples[start:start + 4800]
        mean = sum(block) / len(block)
        variance = max(0.0, sum(value * value for value in block) / len(block) - mean * mean)
        maximum = max(maximum, math.sqrt(variance))
    level = 20 * math.log10(maximum / 32768) if maximum else None
    return {"peak_block_rms_dbfs": level, "threshold_dbfs": QUIET_THRESHOLD_DBFS,
            "quiet": level is None or level < QUIET_THRESHOLD_DBFS}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AnalysisUnavailable(Exception):
    pass


class AnalysisBusy(Exception):
    pass


class AnalysisCoordinator:
    def __init__(self, root: Path, store, enabled: bool = True, timeout: float = 120, on_result=None):
        self.root, self.store, self.enabled, self.timeout = root, store, enabled, timeout
        self.directory = root / ".runtime" / "animal_analysis"
        self.assets = root / ".models" / "perch-v2"
        self._condition = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._capturing = False
        self._paused_at = None
        self._paused_seconds = 0.0
        self._buffer = b""
        self._errors = {}
        self.on_result = on_result

    def availability(self) -> str | None:
        if not self.enabled:
            return "Animal identification is disabled."
        try:
            check_assets(self.assets)
            for name in ("numpy", "scipy", "onnxruntime"):
                if importlib.util.find_spec(name) is None:
                    return "Install requirements-animal.txt to enable animal identification."
        except (OSError, ValueError, ImportError):
            return "Run setup_animal_model.py to install the verified Perch model."
        return None

    def _read(self, identifier: str) -> dict | None:
        try:
            path = self.directory / f"{identifier}.json"
            if path.is_symlink():
                raise ValueError("Analysis state must not be a symlink.")
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("recording_id") != identifier:
                raise ValueError("Invalid analysis state.")
            if state.get("state") not in ("queued", "running", "completed", "failed", "skipped"):
                raise ValueError("Invalid analysis state.")
            return state
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"recording_id": identifier, "state": "failed",
                    "error": "Stored analysis is unreadable. Retry identification."}

    def _write(self, state: dict) -> None:
        atomic_write_json(self.directory / f'{state["recording_id"]}.json', state)

    def delete(self, identifier, remove_recording):
        with self._condition:
            state = self._read(identifier)
            if state and state["state"] == "running":
                raise AnalysisBusy("Animal identification is running for this recording. Try deleting it when identification finishes.")
            remove_recording()
            (self.directory / f"{identifier}.json").unlink(missing_ok=True)
            self._errors.pop(identifier, None)
            self._condition.notify_all()

    def status(self, metadata: dict) -> dict:
        with self._condition:
            identifier = metadata["recording_id"]
            if identifier in self._errors:
                return copy.deepcopy(self._errors[identifier])
            state = self._read(identifier)
            valid = state and state.get("sha256") == metadata["sha256"] and state.get("model") == IDENTITY
            if valid and state["state"] in ("completed", "failed", "skipped"):
                return copy.deepcopy(state)
            unavailable = self.availability()
            if unavailable:
                return {"state": "unavailable", "error": unavailable}
            if valid:
                return copy.deepcopy(state)
            return {"state": "not_analyzed", "outdated": state is not None}

    def automatic(self, metadata: dict) -> dict:
        try:
            return self.request(metadata)
        except Exception as exc:
            LOG.warning("Recording saved but animal analysis could not be queued: %s", exc)
            state = {"state": "unavailable", "error": str(exc)}
            if not isinstance(exc, AnalysisUnavailable):
                with self._condition:
                    self._remember_error(metadata["recording_id"], state)
            return state

    def _remember_error(self, identifier: str, state: dict) -> None:
        self._errors[identifier] = state
        # Exceptional in-memory failures must not become an unbounded recording backlog.
        while len(self._errors) > 100:
            self._errors.pop(next(iter(self._errors)))

    def request(self, metadata: dict) -> dict:
        with self._condition:
            identifier = metadata["recording_id"]
            self._errors.pop(identifier, None)
            state = self.status(metadata)
            if state["state"] == "skipped":
                return state
            if state["state"] in ("queued", "running", "completed"):
                self.start()
                return state
            unavailable = self.availability()
            if unavailable or self._stop.is_set():
                raise AnalysisUnavailable(unavailable or "Animal analyzer is stopping.")
            checked, data = self.store.read(identifier)
            if checked["sha256"] != metadata["sha256"]:
                raise ValueError("Recording changed since analysis was queued.")
            level = audio_level(data)
            if level["quiet"]:
                state = {"recording_id": identifier, "sha256": metadata["sha256"],
                         "model": IDENTITY, "state": "skipped", "completed_at": now(),
                         "reason": "Audio too quiet for animal identification.", "audio_level": level}
                self._write(state)
                if self.on_result:
                    self.on_result(metadata, state)
                return copy.deepcopy(state)
            unavailable = self.availability()
            if unavailable or self._stop.is_set():
                raise AnalysisUnavailable(unavailable or "Animal analyzer is stopping.")
            # Recover existing work before publishing this new job.
            self.start()
            state = {"recording_id": identifier, "sha256": metadata["sha256"],
                     "model": IDENTITY, "state": "queued", "queued_at": now()}
            self._write(state)
            self._condition.notify_all()
            return copy.deepcopy(state)

    def _jobs(self):
        for path in self.directory.glob("*.json"):
            if JOB_NAME.fullmatch(path.name):
                state = self._read(path.stem)
                if state and state["state"] in ("queued", "running"):
                    yield state

    def start(self) -> None:
        with self._condition:
            if self._thread is not None or self._stop.is_set() or self.availability():
                return
            for state in self._jobs():
                state.update(state="queued", model=IDENTITY)
                self._write(state)
            self._thread = threading.Thread(target=self._run, name="animal-analysis", daemon=True)
            self._thread.start()

    @contextmanager
    def capture(self):
        with self._condition:
            self._capturing = True
            self._pause()
        try:
            yield
        finally:
            with self._condition:
                self._capturing = False
                self._resume()
                self._condition.notify_all()

    def _pause(self) -> None:
        if self._process is not None and self._process.poll() is None and self._paused_at is None:
            try:
                self._process.send_signal(signal.SIGSTOP)
                self._paused_at = time.monotonic()
            except ProcessLookupError:
                pass

    def _resume(self) -> None:
        if self._paused_at is not None:
            self._paused_seconds += time.monotonic() - self._paused_at
            self._paused_at = None
            if self._process is not None and self._process.poll() is None:
                try:
                    self._process.send_signal(signal.SIGCONT)
                except ProcessLookupError:
                    pass

    def _pause_total(self) -> float:
        return self._paused_seconds + (time.monotonic() - self._paused_at if self._paused_at else 0)

    def _receive(self) -> dict:
        started = time.monotonic()
        with self._condition:
            paused_before = self._pause_total()
            process = self._process
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not self._stop.is_set():
                with self._condition:
                    paused = self._pause_total() - paused_before
                    if time.monotonic() - started - paused > self.timeout:
                        raise TimeoutError("Animal analysis exceeded its active-time limit.")
                    if self._capturing:
                        self._condition.wait(0.1)
                        continue
                if b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid analyzer response.")
                    if payload.get("error"):
                        raise RuntimeError(str(payload["error"]))
                    return payload
                if selector.select(0.1):
                    block = os.read(process.stdout.fileno(), 65536)
                    if not block:
                        raise RuntimeError("Animal analyzer exited unexpectedly.")
                    self._buffer += block
                    if len(self._buffer) > 1024 * 1024:
                        raise ValueError("Analyzer response is too large.")
            raise RuntimeError("Animal analyzer stopped.")

    def _boot(self) -> None:
        with self._condition:
            if self._stop.is_set():
                raise RuntimeError("Animal analyzer stopped.")
            if self._process is not None and self._process.poll() is None:
                return
            self._process = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, bufsize=0,
                                             env={**os.environ, "OMP_NUM_THREADS": "1",
                                                  "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
            self._buffer = b""
            if self._capturing:
                self._pause()
        if self._receive().get("ready") is not True:
            raise RuntimeError("Animal analyzer did not initialize.")

    def _command(self) -> list[str]:
        return [sys.executable, str(Path(__file__).with_name("animal_worker.py")),
                "--assets", str(self.assets), "--recordings", str(self.store.directory)]

    def _terminate(self) -> None:
        with self._condition:
            self._resume()
            process, self._process = self._process, None
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                else:
                    process.wait()
                process.stdin.close()
                process.stdout.close()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                with self._condition:
                    if self._capturing:
                        self._condition.wait(0.1)
                        continue
                    state = min(self._jobs(), key=lambda job: (job.get("queued_at", ""), job["recording_id"]),
                                default=None)
                    if state is None:
                        self._condition.wait(1)
                        continue
                    state.update(state="running", started_at=now())
                    self._write(state)
                started = time.monotonic()
                metadata = None
                with self._condition:
                    pause_start = self._pause_total()
                try:
                    metadata, data = self.store.read(state["recording_id"])
                    if metadata["sha256"] != state["sha256"]:
                        raise ValueError("Recording changed since analysis was queued.")
                    level = audio_level(data)
                    if level["quiet"]:
                        state.update(state="skipped", reason="Audio too quiet for animal identification.",
                                     audio_level=level)
                    else:
                        self._boot()
                        self._process.stdin.write((json.dumps({"recording_id": state["recording_id"],
                                                              "sha256": state["sha256"]}) + "\n").encode())
                        result = self._receive()
                        if not isinstance(result.get("candidates"), list) or not isinstance(result.get("windows"), list):
                            raise ValueError("Analyzer returned incomplete results.")
                        state.update(state="completed", candidates=result["candidates"], windows=result["windows"])
                except Exception as exc:
                    self._terminate()
                    if self._stop.is_set():
                        state["state"] = "queued"
                    else:
                        state.update(state="failed", error=str(exc))
                with self._condition:
                    state.update(completed_at=now(),
                                 processing_seconds=max(0, time.monotonic() - started -
                                                        (self._pause_total() - pause_start)))
                    try:
                        self._write(state)
                    except OSError as exc:
                        self._remember_error(state["recording_id"], {"state": "failed", "error": str(exc)})
                        LOG.warning("Could not persist animal analysis: %s", exc)
                        # Avoid repeatedly executing an unwritable running job.
                        return
                    if self.on_result and metadata and state["state"] in ("completed", "failed", "skipped"):
                        try:
                            self.on_result(metadata, state)
                        except Exception as exc:
                            LOG.warning("Could not log recording analysis: %s", exc)
        except Exception as exc:
            LOG.exception("Animal queue stopped: %s", exc)
            with self._condition:
                if 'state' in locals() and state:
                    self._remember_error(state["recording_id"], {"state": "failed", "error": str(exc)})
        finally:
            self._terminate()
            with self._condition:
                self._thread = None

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._resume()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._terminate()
