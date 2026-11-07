import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from reading_history import ReadingHistory
from telemetry import NoRedirect, Telemetry


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sender = Telemetry(self.root)
        self.entry = {"read_at": "2026-11-06T00:00:00Z", "http_status": 503,
                      "result": {"status": "unavailable", "error": {"code": "sensor_unavailable"}}}

    def enable(self):
        self.sender.root.mkdir()
        self.sender.token_file.write_text("test-secret", encoding="utf-8")

    def rows(self):
        with self.sender._connect() as db:
            return db.execute("SELECT body FROM events ORDER BY seq").fetchall()

    def opener(self, ack):
        response = Mock(status=200)
        response.read.return_value = json.dumps(ack).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return Mock(open=Mock(return_value=response))

    def test_disabled_does_not_create_queue_or_thread(self):
        self.sender.enqueue("light", self.entry)
        self.sender.start(lambda: {})
        self.assertFalse(self.sender.database.exists())
        self.assertIsNone(self.sender.thread)

    def test_failed_upload_survives_restart_and_ack_deletes_only_sent_rows(self):
        self.enable()
        self.sender.enqueue("light", self.entry)
        event = json.loads(self.rows()[0][0])
        failure = Mock()
        failure.open.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.sender.send_once(failure)
        self.sender = Telemetry(self.root)
        self.assertEqual(json.loads(self.rows()[0][0]), event)
        # New readings after a lost response must not change the persisted batch.
        self.sender.enqueue("birdcast", self.entry)
        ack = self.opener({"accepted": [event["id"]]})
        original_open = ack.open

        def upload(*args, **kwargs):
            self.sender.enqueue("radar", self.entry)
            return original_open(*args, **kwargs)

        ack.open = upload
        self.assertTrue(self.sender.send_once(ack))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual([json.loads(row[0])["source"] for row in self.rows()], ["birdcast", "radar"])
        request = original_open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(json.loads(request.data)["events"][0]["http_status"], 503)

    def test_wrong_ack_keeps_event(self):
        self.enable()
        self.sender.enqueue("light", self.entry)
        with self.assertRaises(ValueError):
            self.sender.send_once(self.opener({"accepted": ["unrelated"]}))
        self.assertEqual(len(self.rows()), 1)

    def test_history_forwards_failed_reads_and_deduplicates_animal_results(self):
        self.enable()
        history = ReadingHistory(self.root, on_entry=self.sender.enqueue)
        history.append("light", 503, self.entry["result"])
        metadata = {"recording_id": "recording-one", "created_at": self.entry["read_at"], "sha256": "abc"}
        analysis = {"state": "completed", "candidates": [
            {"scientific_name": "Test animal", "common_name": "Animal", "score": -2.5}]}
        history.recording(metadata, analysis)
        history.recording(metadata, analysis)
        events = [json.loads(row[0]) for row in self.rows()]
        self.assertEqual([e["source"] for e in events], ["light", "microphone"])
        self.assertEqual(events[1]["result"]["animals"][0]["score"], -2.5)
        self.assertNotIn("audio", events[1]["result"])

    def test_redirect_is_not_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_lamp_sampler_and_uploader_stop_cleanly(self):
        self.enable()
        sampled = threading.Event()
        self.sender.send_once = Mock(return_value=0)

        def snapshot():
            sampled.set()
            return {"directionality": {"angle_degrees": 120}, "lighting": {"brightness": None}}

        self.sender.start(snapshot)
        try:
            self.assertTrue(sampled.wait(2))
        finally:
            self.sender.stop()
        self.assertFalse(self.sender.thread.is_alive())
        self.assertFalse(self.sender.lamp_thread.is_alive())
        event = json.loads(self.rows()[0][0])
        self.assertEqual(event["source"], "lamp")
        self.assertEqual(event["result"]["directionality"]["angle_degrees"], 120)
        self.assertIsNone(event["result"]["lighting"]["brightness"])


if __name__ == "__main__":
    unittest.main()
