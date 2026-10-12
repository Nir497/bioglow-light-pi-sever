"""Validated WAV storage shared by capture, download, and deletion requests."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path

SECONDS = 10
RATE = 48_000
FRAMES = SECONDS * RATE
PCM_BYTES = FRAMES * 2
WAV_BYTES = 44 + PCM_BYTES


class RecordingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RecordingNotFound(RecordingError):
    def __init__(self):
        super().__init__("recording_not_found", "The requested recording is not available.")


class RecordingStore:
    def __init__(self, directory: Path):
        self.directory = directory.absolute()

    def path(self, identifier: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise RecordingNotFound()
        path = self.directory / f"{identifier}.wav"
        if self.directory.resolve() != self.directory or path.resolve() != path:
            raise RecordingError("invalid_audio", "Recording paths must not contain symlinks.")
        return path

    def read(self, identifier: str) -> tuple[dict, bytes]:
        path = self.path(identifier)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != WAV_BYTES:
                    raise RecordingError("invalid_audio", "Recording has an unexpected file type or byte length.")
                data = source.read(WAV_BYTES + 1)
        except FileNotFoundError as exc:
            raise RecordingNotFound() from exc
        except OSError as exc:
            raise RecordingError("transfer_failure", "Recording could not be read.") from exc
        if len(data) != WAV_BYTES:
            raise RecordingError("invalid_audio", "Recording is incomplete.")
        # Require canonical PCM; frame-count metadata alone cannot detect truncated audio.
        if (data[:4] != b"RIFF" or data[8:16] != b"WAVEfmt "
                or struct.unpack("<I", data[4:8])[0] != WAV_BYTES - 8
                or struct.unpack("<IHHIIHH", data[16:36]) != (16, 1, 1, RATE, RATE * 2, 2, 16)
                or data[36:40] != b"data" or struct.unpack("<I", data[40:44])[0] != PCM_BYTES):
            raise RecordingError("invalid_audio", "Recording is not a 10-second mono 48 kHz PCM WAV.")
        try:
            with wave.open(io.BytesIO(data), "rb") as audio:
                if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth(),
                        audio.getnframes(), audio.getcomptype()) != (1, RATE, 2, FRAMES, "NONE"):
                    raise RecordingError("invalid_audio", "Recording has an invalid WAV format.")
                if len(audio.readframes(FRAMES + 1)) != PCM_BYTES:
                    raise RecordingError("invalid_audio", "Recording is missing PCM frames.")
        except (wave.Error, EOFError) as exc:
            raise RecordingError("invalid_audio", "Recording WAV validation failed.") from exc
        digest = hashlib.sha256(data).hexdigest()
        created_at = datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        sidecar = path.with_suffix(".json")
        if sidecar.exists() or sidecar.is_symlink():
            try:
                if sidecar.is_symlink():
                    raise ValueError("symlink metadata")
                known = json.loads(sidecar.read_text(encoding="utf-8"))
                if known["sha256"] != digest or known["byte_length"] != len(data):
                    raise ValueError("changed audio")
                created_at = known["created_at"]
                if not isinstance(created_at, str):
                    raise ValueError("invalid timestamp")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RecordingError("integrity_failed", "Recording or metadata changed; source preserved.") from exc
        metadata = {
            "status": "ok", "recording_id": identifier, "created_at": created_at,
            "content_type": "audio/wav", "duration_seconds": SECONDS, "sample_rate": RATE,
            "channels": 1, "bits_per_sample": 16, "sample_width_bytes": 2,
            "frame_count": FRAMES, "frames": FRAMES, "byte_length": len(data), "sha256": digest,
            "download_path": f"/v1/recordings/{identifier}",
            "download_url": f"/v1/recordings/{identifier}",
        }
        return metadata, data

    def publish_metadata(self, metadata: dict) -> None:
        from birdcast_scraper import atomic_write_json
        atomic_write_json(self.path(metadata["recording_id"]).with_suffix(".json"), metadata)

    def delete(self, identifier: str) -> None:
        path = self.path(identifier)
        sidecar = path.with_suffix(".json")
        if sidecar.is_symlink():
            raise RecordingError("invalid_audio", "Recording metadata must not contain symlinks.")
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise RecordingError("invalid_audio", "Recording must be a regular file.")
            path.unlink()
            sidecar.unlink(missing_ok=True)
        except FileNotFoundError as exc:
            raise RecordingNotFound() from exc
        except OSError as exc:
            raise RecordingError("delete_failed", "Recording could not be deleted.") from exc

    def list_recordings(self) -> list[dict]:
        recordings = []
        if self.directory.resolve() != self.directory:
            raise RecordingError("invalid_audio", "Recording directory must not contain symlinks.")
        for path in self.directory.glob("*.wav"):
            try:
                metadata, _ = self.read(path.stem)
                recordings.append(metadata)
            except RecordingError:
                continue
        return sorted(recordings, key=lambda item: (item["created_at"], item["recording_id"]), reverse=True)
