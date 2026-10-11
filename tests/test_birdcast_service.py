import json
import hashlib
import os
import sys
import threading
import unittest
import http.client
import tempfile
import textwrap
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from birdcast_service import BusyError, RecordingError, Service, SUPPORTED_REGIONS, make_handler, RECORDING_BYTES


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"status": "ok", "region": {"code": "US-CA"}, "metrics": {"birds_in_flight": None}, "warnings": ["missing"]}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.service = Service(self.root, self.root / "config.json", runner=lambda region: self.snapshot)

    def test_supported_regions_and_success_preserve_nulls_warnings(self):
        self.assertIn("US-CA", SUPPORTED_REGIONS)
        result = self.service.snapshot("US-CA")
        self.assertIsNone(result["metrics"]["birds_in_flight"])
        self.assertEqual(result["warnings"], ["missing"])

    def test_busy_request(self):
        gate = threading.Event(); release = threading.Event()
        def runner(region): gate.set(); release.wait(1); return self.snapshot
        service = Service(self.root, self.root / "config.json", runner=runner)
        thread = threading.Thread(target=lambda: service.snapshot("US-CA")); thread.start(); gate.wait(1)
        with self.assertRaises(BusyError): service.snapshot("US-CA")
        release.set(); thread.join()

    def test_scraper_and_no_data_failures_are_distinguishable(self):
        failing = Service(self.root, self.root / "config.json", runner=lambda region: (_ for _ in ()).throw(RuntimeError("boom")))
        with self.assertRaises(RuntimeError): failing.snapshot("US-CA")
        unavailable = Service(self.root, self.root / "config.json", runner=lambda region: (_ for _ in ()).throw(LookupError()))
        with self.assertRaises(LookupError): unavailable.snapshot("US-CA")

    def test_http_validation_and_success_response(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            conn.request("POST", "/v1/snapshots", "not-json", {"Content-Type": "application/json"})
            response = conn.getresponse(); self.assertEqual(response.status, 400); self.assertEqual(json.loads(response.read())["error"]["code"], "malformed_request")
            conn.request("POST", "/v1/snapshots", json.dumps({"region": "CA"}), {"Content-Type": "application/json"})
            response = conn.getresponse(); self.assertEqual(response.status, 400); response.read()
            conn.request("POST", "/v1/snapshots", json.dumps({"region": "US-CA"}), {"Content-Type": "application/json"})
            response = conn.getresponse(); self.assertEqual(response.status, 200); self.assertEqual(json.loads(response.read())["status"], "ok")
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_health_check_does_not_run_scraper(self):
        calls = []
        service = Service(self.root, self.root / "config.json", runner=lambda region: calls.append(region))
        service.light_sensor.response = lambda health=False: (200, {"status": "ok", "sensor_connected": True})
        service.presence_sensor.response = lambda health=False: (200, {"status": "ok", "serial_connected": True, "fresh": True})
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            conn.request("GET", "/health")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["status"], "ok")
            self.assertEqual(calls, [])
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def recorder_script(self, directory, mode="valid"):
        script = Path(directory) / "recorder.py"
        script.write_text(textwrap.dedent(f"""
            import sys, wave
            output = sys.argv[sys.argv.index('--output') + 1]
            if {mode!r} == 'fail':
                print('hardware unavailable', file=sys.stderr); raise SystemExit(7)
            if {mode!r} == 'missing':
                print('/tmp/not-the-requested-file.wav'); raise SystemExit(0)
            with wave.open(output, 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(48000)
                wav.writeframes(b'\\0' * {RECORDING_BYTES})
            print(output)
        """), encoding="utf-8")
        return str(script)

    def test_recording_metadata_download_integrity_and_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), Path(directory) / "config.json", recorder=self.recorder_script(directory))
            metadata = service.start_recording()
            self.assertEqual(metadata["frame_count"], 480000)
            self.assertEqual(metadata["byte_length"], 44 + RECORDING_BYTES)
            details, data = service.recording(metadata["recording_id"])
            self.assertEqual(len(data), details["byte_length"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), details["sha256"])
            service.finish_transfer()
            service.delete_recording(metadata["recording_id"])
            retained, again = service.recording(metadata["recording_id"])
            self.assertEqual(again, data)
            self.assertEqual(retained, metadata)

    def test_recording_rejects_failures_and_untrusted_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode, code in (("fail", "recorder_failed"), ("missing", "missing_output")):
                service = Service(Path(directory), Path(directory) / "config.json", recorder=self.recorder_script(directory, mode))
                with self.assertRaises(RecordingError) as raised:
                    service.start_recording()
                self.assertEqual(raised.exception.code, code)

    def test_sequential_recordings_do_not_require_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), Path(directory) / "config.json", recorder=self.recorder_script(directory))
            first = service.start_recording()
            second = service.start_recording()
            self.assertNotEqual(first["recording_id"], second["recording_id"])
            self.assertEqual(len(service.recording_store.list_recordings()), 2)

    def test_http_snapshot_and_recording_workflows_share_service(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(
                Path(directory), Path(directory) / "config.json",
                runner=lambda region: self.snapshot,
                recorder=self.recorder_script(directory),
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request("POST", "/v1/snapshots", json.dumps({"region": "US-CA"}), {"Content-Type": "application/json"})
                response = conn.getresponse(); self.assertEqual(response.status, 200); self.assertEqual(json.loads(response.read())["status"], "ok")
                conn.request("POST", "/v1/recordings")
                response = conn.getresponse(); self.assertEqual(response.status, 200)
                metadata = json.loads(response.read())
                conn.request("GET", metadata["download_path"])
                response = conn.getresponse(); audio = response.read()
                self.assertEqual(response.status, 200); self.assertEqual(len(audio), metadata["byte_length"])
                conn.request("DELETE", metadata["download_path"])
                response = conn.getresponse(); self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["retained"], True)
                conn.request("POST", "/v1/snapshots", json.dumps({"region": "US-CA"}), {"Content-Type": "application/json"})
                response = conn.getresponse(); self.assertEqual(response.status, 200)
            finally:
                server.shutdown(); thread.join(); server.server_close()

if __name__ == "__main__":
    unittest.main()
