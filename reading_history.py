"""Append-only, timestamped JSON lines in human-readable sensor text files."""
import json
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


class ReadingHistory:
    FILES = {"radar": "presence.txt", "light": "luminance.txt", "birdcast": "migration.txt",
             "microphone": "recordings.txt"}
    RANGES = {"30m": 1800, "1h": 3600, "6h": 21600, "12h": 43200,
              "1d": 86400, "7d": 604800, "all": None}

    def __init__(self, root: Path):
        self.root = root / "readings"
        self.lock = threading.Lock()
        self.recording_keys = None
        self.recording_lock = threading.Lock()

    def _entries(self, source):
        try:
            with (self.root / self.FILES[source]).open("rb") as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                        if (isinstance(entry, dict) and isinstance(entry.get("result"), dict)
                                and isinstance(entry.get("read_at"), str)
                                and isinstance(entry.get("http_status"), int)):
                            yield entry
                    except (ValueError, UnicodeDecodeError):
                        continue
        except FileNotFoundError:
            return

    def series(self, source, window="1h", region=None, max_points=1000):
        end = datetime.now(timezone.utc).timestamp()
        seconds = self.RANGES[window]
        start = end - seconds if seconds else float("-inf")
        count, earliest, animals = 0, end, set()
        recordings = None
        if source == "microphone":
            recordings = {}
            for entry in self._entries(source):
                identifier = entry["result"].get("recording_id")
                if identifier:
                    recordings[identifier] = entry

        def points():
            for entry in recordings.values() if recordings is not None else self._entries(source):
                if source == "birdcast" and region:
                    recorded_region = entry["result"].get("region")
                    if not isinstance(recorded_region, dict) or recorded_region.get("code") != region:
                        continue
                stamp = entry["result"].get("timestamp") if source == "microphone" else entry["read_at"]
                try:
                    timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError, AttributeError):
                    continue
                if math.isfinite(timestamp) and start <= timestamp <= end:
                    yield timestamp, entry

        for timestamp, entry in points():
            earliest = min(earliest, timestamp)
            count += 1
            for animal in entry["result"].get("animals", []) or []:
                if isinstance(animal, dict) and isinstance(animal.get("scientific_name"), str):
                    animals.add(animal["scientific_name"])
        start = earliest if seconds is None else start
        if count <= max_points:
            return {"entries": [entry for _, entry in sorted(points(), key=lambda item: item[0])],
                    "total": count, "sampled": False, "animals": sorted(animals),
                    "range": window, "start": start if count else end, "end": end}
        buckets = {}
        # Keep the last reading in each time bucket, bounding browser payloads.
        for timestamp, entry in points():
            bucket = min(max_points - 1, int((timestamp - start) / max(1, end - start) * max_points))
            if bucket not in buckets or timestamp >= buckets[bucket][0]:
                buckets[bucket] = (timestamp, entry)
        return {"entries": [entry for _, entry in sorted(buckets.values(), key=lambda item: item[0])],
                "total": count, "sampled": count > len(buckets), "animals": sorted(animals),
                "range": window, "start": start if count else end, "end": end}

    def recording(self, metadata, analysis):
        animals = {}
        candidates = list(analysis.get("candidates", []))
        for window in analysis.get("windows", []):
            candidates.extend(window.get("candidates", []))
        for candidate in candidates:
            name = candidate["scientific_name"]
            if name not in animals or candidate["score"] > animals[name]["score"]:
                animals[name] = candidate.copy()
        result = {"recording_id": metadata["recording_id"], "timestamp": metadata["created_at"],
                  "sha256": metadata["sha256"], "state": analysis.get("state", "not_analyzed"),
                  "analyzed_at": analysis.get("completed_at"), "model": analysis.get("model"),
                  "animals": sorted(animals.values(), key=lambda animal: (-animal["score"], animal["scientific_name"]))}
        if analysis.get("error"):
            result["error"] = analysis["error"]
        key = json.dumps(result, sort_keys=True)
        # A separate lock keeps repeated completion notifications idempotent.
        with self.recording_lock:
            if self.recording_keys is None:
                self.recording_keys = {json.dumps(entry["result"], sort_keys=True)
                                       for entry in self._entries("microphone")}
            if key in self.recording_keys:
                return
            self.append("microphone", 200 if result["state"] == "completed" else 202, result)
            self.recording_keys.add(key)

    def append(self, source, status, result):
        entry = {"read_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                 "http_status": status, "result": result}
        line = json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n"
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / self.FILES[source]).open("a+b") as stream:
                # Preserve a truncated last record after an interrupted write,
                # while ensuring the next reading starts on a separate line.
                stream.seek(0, 2)
                if stream.tell():
                    stream.seek(-1, 2)
                    if stream.read(1) != b"\n":
                        stream.write(b"\n")
                stream.write(line.encode("utf-8"))

    def delete_recording(self, identifier):
        with self.recording_lock, self.lock:
            path = self.root / self.FILES["microphone"]
            if not path.exists():
                return
            temporary = None
            try:
                with path.open("rb") as source, tempfile.NamedTemporaryFile(dir=self.root, delete=False) as target:
                    temporary = Path(target.name)
                    for line in source:
                        try:
                            entry = json.loads(line)
                            if isinstance(entry, dict) and entry.get("result", {}).get("recording_id") == identifier:
                                continue
                        except (ValueError, UnicodeDecodeError, AttributeError):
                            pass
                        target.write(line)
                os.replace(temporary, path)
                self.recording_keys = None
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)

    def recent(self, source, limit=500):
        # Read from the end so permanent logs do not slow each dashboard refresh.
        with self.lock:
            try:
                with (self.root / self.FILES[source]).open("rb") as stream:
                    stream.seek(0, 2)
                    position = stream.tell()
                    chunks, count = [], 0
                    while position and count <= limit:
                        size = min(position, 8192)
                        position -= size
                        stream.seek(position)
                        chunk = stream.read(size)
                        chunks.append(chunk)
                        count += chunk.count(b"\n")
                lines = b"".join(reversed(chunks)).splitlines()[-limit:]
            except FileNotFoundError:
                return []
        entries = []
        for line in lines:
            try:
                entry = json.loads(line)
                if (isinstance(entry, dict) and isinstance(entry.get("result"), dict)
                        and isinstance(entry.get("read_at"), str)
                        and isinstance(entry.get("http_status"), int)):
                    entries.append(entry)
            except (ValueError, UnicodeDecodeError):
                continue
        return entries

    def all_recent(self):
        return {source: self.recent(source) for source in self.FILES}
