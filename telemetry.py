"""Optional, durable outbound telemetry. No hardware reads or audio uploads."""
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener


ENDPOINT = "https://bioglow-data.orengolov.workers.dev/v1/events"
LOGGER = logging.getLogger(__name__)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the upload credential to a redirect target.
        return None


class Telemetry:
    def __init__(self, root: Path):
        self.root = root / ".runtime"
        self.token_file = self.root / "telemetry-token"
        self.database = self.root / "telemetry.sqlite3"
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None

    @contextmanager
    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=5)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS in_flight (id INTEGER PRIMARY KEY CHECK(id = 1), body TEXT NOT NULL)")
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, source, entry):
        # Missing token means disabled: no new queue, no background network traffic.
        if not self.token_file.is_file():
            return
        try:
            event = {"id": str(uuid.uuid4()), "station": "bioglow-pi", "source": source, **entry}
            body = json.dumps(event, ensure_ascii=False, allow_nan=False)
            with self.lock, self._connect() as db:
                db.execute("INSERT INTO events(body) VALUES (?)", (body,))
        except (OSError, ValueError, sqlite3.Error):
            LOGGER.exception("Could not queue telemetry event; local sensor history is retained")

    def send_once(self, opener=None):
        token = self.token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("Telemetry token is empty")
        with self.lock, self._connect() as db:
            pending = db.execute("SELECT body FROM in_flight WHERE id = 1").fetchone()
            if pending:
                batch = json.loads(pending[0])
            else:
                rows = db.execute("SELECT seq, body FROM events ORDER BY seq LIMIT 50").fetchall()
                if not rows:
                    return 0
                batch = {"seqs": [seq for seq, _ in rows],
                         "events": [json.loads(body) for _, body in rows]}
                db.execute("INSERT INTO in_flight(id, body) VALUES (1, ?)", (json.dumps(batch),))
        events = batch["events"]
        body = json.dumps({"events": events}, allow_nan=False).encode("utf-8")
        request = Request(ENDPOINT, data=body, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
        with (opener or build_opener(NoRedirect())).open(request, timeout=10) as response:
            ack = json.loads(response.read(4096))
            if response.status != 200 or ack.get("accepted") != [event["id"] for event in events]:
                raise ValueError("Worker did not acknowledge this batch")
        # A lost response leaves rows queued. Worker deduplication makes retry safe.
        with self.lock, self._connect() as db:
            db.executemany("DELETE FROM events WHERE seq = ?", [(seq,) for seq in batch["seqs"]])
            db.execute("DELETE FROM in_flight WHERE id = 1")
        return len(events)

    def start(self, lamp_snapshot):
        if self.thread or not self.token_file.is_file():
            return
        self.stopped.clear()

        def collect():
            while not self.stopped.is_set():
                try:
                    self.enqueue("lamp", {
                        "read_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "http_status": 200, "result": lamp_snapshot()})
                except Exception:
                    LOGGER.exception("Could not sample lamp telemetry")
                self.stopped.wait(1)

        def send():
            delay = 5
            while not self.stopped.is_set():
                try:
                    sent = self.send_once()
                    delay = 5
                    self.stopped.wait(0.1 if sent == 50 else 5)
                except Exception as exc:
                    # Do not log request objects, headers, or the credential.
                    LOGGER.warning("Telemetry upload failed (%s); queued events will retry", type(exc).__name__)
                    self.stopped.wait(delay)
                    delay = min(60, delay * 2)

        self.thread = threading.Thread(target=send, name="telemetry-upload", daemon=True)
        self.lamp_thread = threading.Thread(target=collect, name="telemetry-lamp", daemon=True)
        self.thread.start()
        self.lamp_thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=12)
            self.lamp_thread.join(timeout=2)
