"""Append-only, timestamped JSON lines in human-readable sensor text files."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class ReadingHistory:
    FILES = {"radar": "presence.txt", "light": "luminance.txt", "birdcast": "migration.txt"}

    def __init__(self, root: Path):
        self.root = root / "readings"
        self.lock = threading.Lock()

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
