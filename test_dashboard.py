"""Dashboard and retention tests independent of HTTP sockets and real hardware."""
import hashlib
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from birdcast_service import BusyError, RecordingError, Service, make_handler, RECORDING_FRAMES


class DashboardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.snapshot = {"status": "ok", "region": {"code": "US-CA"},
                         "retrieved_at": "2026-10-11T12:00:00Z",
                         "metrics": {"birds_in_flight": 123}, "warnings": []}
        self.service = Service(self.root, self.root / "config.json", runner=lambda _: self.snapshot)

    def request(self, method, path, body=None, headers=None):
        handler = object.__new__(make_handler(self.service))
        handler.command, handler.path = method, path
        handler.connection = Mock()
        raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
        handler.headers = headers if headers is not None else {"Content-Length": str(len(raw))}
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        statuses, response_headers = [], {}
        handler.send_response = statuses.append
        handler.send_header = lambda key, value: response_headers.update({key: value})
        handler.end_headers = lambda: None
        getattr(handler, f"do_{method}")()
        return statuses[0], response_headers, handler.wfile.getvalue()

    def record(self, command, **kwargs):
        with wave.open(command[-1], "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(48000)
            audio.writeframes(b"\x01\x00" * RECORDING_FRAMES)
        return subprocess.CompletedProcess(command, 0, str(command[-1]) + "\n", "")

    def capture(self):
        with patch("birdcast_service.subprocess.run", side_effect=self.record):
            return self.service.start_recording()

    def test_dashboard_assets_and_empty_data_never_probe_hardware(self):
        self.service.light_sensor.response = Mock(side_effect=AssertionError("hardware probe"))
        self.service.runner = Mock(side_effect=AssertionError("scrape"))
        with patch("birdcast_service.subprocess.run", side_effect=AssertionError("record")):
            for path, content_type in [("/", "text/html"), ("/distance-test", "text/html"), ("/dashboard.css", "text/css"),
                                       ("/dashboard.js", "text/javascript"), ("/favicon.png", "image/png"),
                                       ("/glow-favicon.png?v=d359af34", "image/png"),
                                       ("/favicon.ico", "image/png")]:
                status, headers, data = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(content_type))
                self.assertGreater(len(data), 100)
                if content_type == "image/png":
                    self.assertEqual(data, (Path(__file__).parent / "web" / "favicon.png").read_bytes())
                    self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
                if path == "/":
                    self.assertIn(b'<link rel="icon" type="image/png" href="/glow-favicon.png?v=d359af34">', data)
            status, _, data = self.request("GET", "/v1/latest")
            self.assertEqual(status, 200)
            latest = json.loads(data)
            self.assertNotIn("light", latest["sources"])
            self.assertEqual(latest["sources"]["radar"]["http_status"], 503)
            self.assertTrue(any(item["code"] == "US-CA" for item in latest["regions"]))
            self.assertEqual(json.loads(self.request("GET", "/v1/recordings")[2])["recordings"], [])

    def test_latest_failure_retains_historical_result_and_survives_restart(self):
        self.service.snapshot("US-CA")
        self.service.runner = Mock(side_effect=RuntimeError("offline"))
        with self.assertRaises(RuntimeError):
            self.service.snapshot("US-CA")
        restarted = Service(self.root, self.root / "config.json")
        entry = restarted.latest()["sources"]["birdcast"]
        self.assertEqual(entry["http_status"], 502)
        self.assertEqual(entry["result"]["error"]["message"], "offline")
        self.assertEqual(entry["last_success"], self.snapshot)
        self.assertTrue(entry["requested_at"].endswith("Z"))
        entry["last_success"]["metrics"]["birds_in_flight"] = 999
        self.assertEqual(restarted.latest()["sources"]["birdcast"]["last_success"]["metrics"]["birds_in_flight"], 123)

    def test_light_reads_and_failure_are_cached(self):
        reading = {"lux": 42.5, "sensor": "VEML7700", "timestamp": "2026-10-11T12:00:00Z"}
        self.service.light_sensor.response = Mock(return_value=(200, reading))
        self.assertEqual(self.request("GET", "/reading")[0], 200)
        unavailable = {"status": "error", "error": {"code": "sensor_unavailable", "message": "disconnected"}}
        self.service.light_sensor.response.return_value = (503, unavailable)
        self.assertEqual(self.request("GET", "/reading")[0], 503)
        entry = self.service.latest()["sources"]["light"]
        self.assertEqual(entry["http_status"], 503)
        self.assertEqual(entry["last_success"], reading)
        self.assertEqual(entry["result"], unavailable)

    def test_passive_radar_freshness_is_recomputed(self):
        radar = self.service.presence_sensor
        with radar.lock:
            radar.connected = True
            radar.latest = {"presence": "moving", "timestamp": "2026-10-11T12:00:00Z",
                            "moving_distance_cm": 123}
            radar.received_at = time.monotonic()
        self.assertEqual(self.service.latest()["sources"]["radar"]["result"]["presence"], "moving")
        with radar.lock:
            radar.received_at = time.monotonic() - 3
        entry = self.service.latest()["sources"]["radar"]
        self.assertEqual(entry["http_status"], 503)
        self.assertFalse(entry["result"]["fresh"])
        self.assertNotIn("presence", entry["result"])
        self.assertNotIn("moving_distance_cm", entry["result"])

    def test_text_histories_record_reads_errors_and_survive_restart(self):
        self.service.presence_sensor.response = Mock(return_value=(200, {
            "presence": "stationary", "fresh": True, "timestamp": "2026-10-11T12:00:00Z"}))
        self.service.light_sensor.response = Mock(return_value=(200, {"lux": 42.5}))
        self.request("GET", "/presence")
        self.request("GET", "/reading")
        self.service.snapshot("US-CA")
        self.service.presence_sensor.response.return_value = (503, {"fresh": False, "error": {"message": "stale"}})
        self.request("GET", "/presence")
        for filename in ("presence.txt", "luminance.txt", "migration.txt"):
            lines = (self.root / "readings" / filename).read_text().splitlines()
            self.assertTrue(lines)
            self.assertTrue(json.loads(lines[0])["read_at"].endswith("Z"))
        restarted = Service(self.root, self.root / "config.json")
        history = restarted.reading_history.all_recent()
        self.assertEqual(len(history["radar"]), 2)
        self.assertEqual(history["radar"][0]["result"]["presence"], "stationary")
        self.assertEqual(history["radar"][1]["http_status"], 503)
        self.assertNotIn("presence", history["radar"][1]["result"])
        self.assertEqual(history["light"][0]["result"]["lux"], 42.5)
        self.assertEqual(history["birdcast"][0]["result"]["metrics"]["birds_in_flight"], 123)

    def test_history_tail_ignores_corrupt_records_and_preserves_full_log(self):
        history = self.service.reading_history
        for value in range(510):
            history.append("light", 200, {"lux": value, "padding": "x" * 100})
        recent = history.recent("light")
        self.assertEqual(len(recent), 500)
        self.assertEqual(recent[0]["result"]["lux"], 10)
        path = self.root / "readings" / "luminance.txt"
        with path.open("a") as stream:
            stream.write('{"truncated":')
        history.append("light", 200, {"lux": 999})
        self.assertEqual(history.recent("light")[-1]["result"]["lux"], 999)
        self.assertEqual(len(path.read_text().splitlines()), 512)

    def test_history_write_failure_is_visible_without_losing_reading(self):
        self.service.light_sensor.response = Mock(return_value=(200, {"lux": 42}))
        with patch.object(self.service.reading_history, "append", side_effect=OSError("full")):
            with self.assertLogs("birdcast_service", level="WARNING"):
                status, _, raw = self.request("GET", "/reading")
        self.assertEqual(status, 200)
        self.assertIn("history_error", json.loads(raw))

    def test_concurrent_reading_logs_keep_every_complete_record(self):
        history = self.service.reading_history
        def write_readings(worker):
            for index in range(30):
                history.append("light", 200, {"lux": worker * 30 + index})
        workers = [threading.Thread(target=write_readings, args=(worker,)) for worker in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        entries = history.recent("light")
        self.assertEqual(len(entries), 120)
        self.assertEqual({entry["result"]["lux"] for entry in entries}, set(range(120)))

    def test_history_ranges_use_full_logs_and_filter_region(self):
        history = self.service.reading_history
        history.root.mkdir()
        now = datetime.now(timezone.utc)
        with (history.root / "luminance.txt").open("w") as stream:
            for index in range(750):
                stream.write(json.dumps({"read_at": (now-timedelta(seconds=2400-index)).isoformat(),
                                         "http_status": 200, "result": {"lux": index}})+"\n")
        hour = history.series("light", "1h")
        self.assertEqual(hour["total"], 750)
        self.assertEqual(len(hour["entries"]), 750)
        self.assertEqual(hour["entries"][0]["result"]["lux"], 0)
        half_hour = history.series("light", "30m")
        self.assertLess(half_hour["total"], 750)
        self.assertGreater(half_hour["entries"][0]["result"]["lux"], 500)
        for region in ("US-CA", "US-NY"):
            history.append("birdcast", 200, {"region": {"code": region}, "metrics": {"birds_in_flight": 5}})
        data = json.loads(self.request("GET", "/v1/history?source=birdcast&range=1d&region=US-NY")[2])
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["entries"][0]["result"]["region"]["code"], "US-NY")
        self.assertEqual(self.request("GET", "/v1/history?source=../../secret&range=1h")[0], 400)
        self.assertEqual(self.request("GET", "/v1/history?source=light&range=invalid")[0], 400)

    def test_history_sampling_is_bounded_and_keeps_full_text_file(self):
        history = self.service.reading_history
        for value in range(1200):
            history.append("light", 200, {"lux": value})
        series = history.series("light", "all", max_points=100)
        self.assertEqual(series["total"], 1200)
        self.assertLessEqual(len(series["entries"]), 100)
        self.assertTrue(series["sampled"])
        self.assertEqual(series["entries"][-1]["result"]["lux"], 1199)
        self.assertEqual(len((history.root/"luminance.txt").read_text().splitlines()), 1200)

    def test_recording_history_keeps_animal_scores_and_no_backfill(self):
        metadata = self.capture()
        analysis = {"state": "completed", "completed_at": datetime.now(timezone.utc).isoformat(),
                    "candidates": [{"scientific_name": "Bubo virginianus", "score": 8.5},
                                   {"scientific_name": "Rana temporaria", "score": -2.1}],
                    "windows": [{"candidates": [{"scientific_name": "Bubo virginianus", "score": 9},
                                                  {"scientific_name": "Strix varia", "score": 4}]}]}
        self.service.animal_analysis.status = Mock(return_value=analysis)
        before = len(self.service.reading_history.recent("microphone"))
        self.service.recordings()
        self.assertEqual(len(self.service.reading_history.recent("microphone")), before,
                         "Reading existing results must not backfill the new log")
        self.service.animal_analysis.on_result(metadata, analysis)
        self.service.animal_analysis.on_result(metadata, analysis)
        series = self.service.reading_history.series("microphone", "all")
        self.assertEqual(series["total"], 1, "The graph uses the latest result per recording")
        entry = series["entries"][0]["result"]
        self.assertEqual(entry["timestamp"], metadata["created_at"])
        self.assertEqual(entry["animals"], [{"scientific_name": "Bubo virginianus", "score": 9},
                                            {"scientific_name": "Strix varia", "score": 4},
                                            {"scientific_name": "Rana temporaria", "score": -2.1}])
        self.assertEqual(series["animals"], ["Bubo virginianus", "Rana temporaria", "Strix varia"])
        restarted = Service(self.root, self.root/"config.json")
        restarted.log_recording(metadata, analysis)
        self.assertEqual(len(restarted.reading_history.recent("microphone")), before+1)

    def test_common_name_endpoint_validates_and_returns_seeded_names_offline(self):
        with patch("animal_names.urlopen", side_effect=AssertionError("network")):
            status, _, raw = self.request("GET", "/v1/animal-names?name=Bubo%20virginianus&name=Buteo%20jamaicensis")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["names"]["Buteo jamaicensis"], "Red-tailed Hawk")
        self.assertEqual(self.request("GET", "/v1/animal-names?name=%3Cscript%3E")[0], 400)

    def test_collection_controls_and_passive_dashboard_do_not_duplicate_sensor_logs(self):
        self.service.light_sensor.response = Mock(return_value=(200, {"lux": 42}))
        self.service.presence_sensor.response = Mock(return_value=(503, {"fresh": False}))
        self.service.runner = Mock(side_effect=AssertionError("BirdCast must remain manual"))
        self.service.collect_sensors()
        self.service.latest()
        self.service.latest()
        self.assertEqual(len(self.service.reading_history.recent("light")), 1)
        self.assertEqual(len(self.service.reading_history.recent("radar")), 1)
        self.assertEqual(self.service.collection_schedule.sensor_interval, 10)
        self.assertEqual(self.service.collection_schedule.light_interval, 1)
        self.assertEqual(self.service.collection_schedule.recording_interval, 120)
        status, _, raw = self.request("POST", "/v1/recording-schedule", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["collection"]["enabled"])
        self.assertEqual(json.loads(raw)["collection"]["duration_seconds"], 5)
        self.assertEqual(self.request("POST", "/v1/recording-schedule", {"enabled": "yes"})[0], 400)
        self.request("POST", "/v1/recording-schedule", {"enabled": False})
        self.assertFalse(self.service.collection_schedule.status()["enabled"])

    def test_corrupt_runtime_cache_does_not_prevent_startup(self):
        self.service._cache_file.parent.mkdir()
        self.service._cache_file.write_text("not json")
        self.assertEqual(set(Service(self.root, self.root / "config.json").latest()["sources"]), {"radar"})

    def test_cache_write_failure_keeps_completed_result_available(self):
        self.service.light_sensor.response = Mock(return_value=(200, {"lux": 42}))
        with patch("birdcast_service.birdcast_scraper.atomic_write_json", side_effect=OSError("disk unavailable")):
            with self.assertLogs("birdcast_service", level="WARNING"):
                self.assertEqual(self.request("GET", "/reading")[0], 200)
        self.assertEqual(self.service.latest()["sources"]["light"]["result"]["lux"], 42)

    def test_malformed_cache_entries_are_ignored(self):
        self.service._cache_file.parent.mkdir()
        self.service._cache_file.write_text(json.dumps({"light": {"http_status": 200, "result": None}}))
        restarted = Service(self.root, self.root / "config.json")
        self.assertNotIn("light", restarted.latest()["sources"])

    def test_scraper_runtime_files_do_not_overwrite_existing_snapshot(self):
        original = self.root / "birdcast_service_snapshot.json"
        original.write_text("keep existing snapshot")
        def run(command, **kwargs):
            config_path = Path(command[command.index("--config") + 1])
            self.assertEqual(config_path.parent, self.root / ".runtime")
            config = json.loads(config_path.read_text())
            Path(config["output_file"]).write_text(json.dumps(self.snapshot))
            return subprocess.CompletedProcess(command, 0, "", "")
        self.service.runner = self.service._run_scraper
        with patch("birdcast_service.subprocess.run", side_effect=run):
            self.service.snapshot("US-CA")
        self.assertEqual(original.read_text(), "keep existing snapshot")

    def test_sequential_capture_listing_and_restart_downloads(self):
        first = self.capture()
        # Set first publication timestamp explicitly so ordering isn't clock-resolution dependent.
        first["created_at"] = "2026-10-01T00:00:00Z"
        self.service.recording_store.publish_metadata(first)
        second = self.capture()
        self.assertEqual(first["download_path"], first["download_url"])
        restarted = Service(self.root, self.root / "config.json")
        self.assertEqual([item["recording_id"] for item in restarted.recording_store.list_recordings()],
                         [second["recording_id"], first["recording_id"]])
        metadata, data = restarted.recording(first["recording_id"])
        self.assertEqual(metadata["created_at"], first["created_at"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), first["sha256"])
        with wave.open(io.BytesIO(data), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getframerate(), audio.getsampwidth(),
                              audio.getnframes()), (1, 48000, 2, 240000))
        self.assertEqual(restarted.recording(first["recording_id"])[1], data)

    def test_delete_removes_audio_metadata_analysis_and_history(self):
        metadata = self.capture()
        analysis_path = self.service.animal_analysis.directory/(metadata["recording_id"]+".json")
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_path.write_text(json.dumps({"recording_id": metadata["recording_id"], "state": "completed"}))
        path = metadata["download_path"]
        status, headers, data = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(headers["X-Content-SHA256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(int(headers["Content-Length"]), len(data))
        status, _, body = self.request("DELETE", path,
                                       {"verified": True, "sha256": metadata["sha256"], "byte_length": len(data)})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok", "recording_id": metadata["recording_id"],
                                           "retained": False, "deleted": True})
        self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("DELETE", path)[0], 404)
        self.assertEqual(len(json.loads(self.request("GET", "/v1/recordings")[2])["recordings"]), 0)
        self.assertFalse(self.service.recording_store.path(metadata["recording_id"]).with_suffix(".json").exists())
        self.assertFalse((self.service.animal_analysis.directory/(metadata["recording_id"]+".json")).exists())
        self.assertEqual(self.service.reading_history.recent("microphone"), [])
        self.assertNotIn("microphone", Service(self.root, self.root/"config.json").latest()["sources"])

    def test_delete_refuses_running_analysis_capture_and_untrusted_paths(self):
        metadata = self.capture()
        identifier = metadata["recording_id"]
        state = {"recording_id": identifier, "state": "running"}
        self.service.animal_analysis._write(state)
        self.assertEqual(self.request("DELETE", metadata["download_path"])[0], 409)
        self.assertTrue(self.service.recording_store.path(identifier).exists())
        self.assertEqual(self.request("DELETE", "/v1/recordings/../../outside")[0], 404)
        self.service.animal_analysis._write({**state, "state": "queued"})
        self.service._recording_lock.acquire()
        try:
            self.assertEqual(self.request("DELETE", metadata["download_path"])[0], 409)
        finally:
            self.service._recording_lock.release()
        self.assertEqual(self.request("DELETE", metadata["download_path"])[0], 200)
        self.assertEqual(list(self.service.animal_analysis._jobs()), [])

    def test_delete_preserves_other_recordings_and_their_animal_history(self):
        first, second = self.capture(), self.capture()
        self.service.delete_recording(first["recording_id"])
        self.assertEqual(self.service.recording(second["recording_id"])[0]["recording_id"], second["recording_id"])
        self.assertEqual({entry["result"]["recording_id"] for entry in self.service.reading_history.recent("microphone")},
                         {second["recording_id"]})
        outside = self.root/"outside.wav"
        outside.write_text("preserve me")
        malicious = self.root/"recordings"/("a"*32+".wav")
        malicious.symlink_to(outside)
        self.assertEqual(self.request("DELETE", "/v1/recordings/"+"a"*32)[0], 500)
        self.assertEqual(outside.read_text(), "preserve me")

    def test_capture_busy_does_not_block_library_or_downloads(self):
        metadata = self.capture()
        started, release = threading.Event(), threading.Event()
        failures = []
        def blocking_recorder(command, **kwargs):
            started.set()
            release.wait(5)
            return self.record(command, **kwargs)
        def capture():
            try:
                self.service.start_recording()
            except Exception as exc:
                failures.append(exc)
        with patch("birdcast_service.subprocess.run", side_effect=blocking_recorder):
            thread = threading.Thread(target=capture)
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                with self.assertRaises(BusyError):
                    self.service.start_recording()
                self.assertEqual(self.request("POST", "/v1/recordings")[0], 409)
                self.assertEqual(self.request("GET", "/v1/recordings")[0], 200)
                self.assertEqual(self.request("GET", metadata["download_path"])[0], 200)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])

    def test_malformed_audio_and_symlinks_are_excluded_and_paths_restricted(self):
        valid = self.capture()
        folder = self.service.recording_dir
        (folder / ("a" * 32 + ".wav")).write_bytes(b"incomplete")
        (folder / ("b" * 32 + ".wav")).symlink_to(folder / (valid["recording_id"] + ".wav"))
        (folder / "private.wav").write_bytes(b"private")
        self.assertEqual(len(self.service.recording_store.list_recordings()), 1)
        for path in ["/v1/recordings/../private", "/v1/recordings/%2e%2e/private", "/v1/recordings/nope",
                     "/v1/recordings/" + "f" * 32, "/web/../birdcast_service.py", "/recordings/private.wav"]:
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("GET", "/v1/recordings/" + "a" * 32)[0], 502)
        self.assertEqual(self.request("GET", "/v1/recordings/" + "b" * 32)[0], 502)

    def test_modified_audio_fails_integrity_even_after_restart(self):
        metadata = self.capture()
        path = self.service.recording_store.path(metadata["recording_id"])
        with path.open("r+b") as audio:
            audio.seek(44)
            audio.write(b"\x02\x00")
        restarted = Service(self.root, self.root / "config.json")
        with self.assertRaises(RecordingError) as raised:
            restarted.recording(metadata["recording_id"])
        self.assertEqual(raised.exception.code, "integrity_failed")
        self.assertEqual(restarted.recording_store.list_recordings(), [])
        self.assertTrue(path.exists())

    def test_truncated_wrong_rate_and_bad_header_are_rejected(self):
        metadata = self.capture()
        path = self.service.recording_store.path(metadata["recording_id"])
        original = path.read_bytes()
        for data in [original[:-2], original[:24] + (44100).to_bytes(4, "little") + original[28:],
                     b"BAD!" + original[4:]]:
            path.write_bytes(data)
            with self.assertRaises(RecordingError) as raised:
                self.service.recording(metadata["recording_id"])
            self.assertEqual(raised.exception.code, "invalid_audio")

    def test_recording_timeout_is_reported_and_capture_lock_released(self):
        with patch("birdcast_service.subprocess.run", side_effect=subprocess.TimeoutExpired("recorder", 40)):
            self.assertEqual(self.request("POST", "/v1/recordings")[0], 502)
        self.assertEqual(self.service.latest()["sources"]["microphone"]["result"]["error"]["code"], "recorder_timeout")
        self.capture()

    def test_malformed_recording_requests_do_not_start_capture(self):
        with patch("birdcast_service.subprocess.run", side_effect=AssertionError("unexpected recording")):
            for body in [{"seconds": 20}, [], b"not json"]:
                self.assertEqual(self.request("POST", "/v1/recordings", body)[0], 400)
            for headers in [{"Content-Length": "bad"}, {"Content-Length": "5000"},
                            {"Transfer-Encoding": "chunked"}]:
                self.assertEqual(self.request("POST", "/v1/recordings", headers=headers)[0], 400)

    def test_invalid_snapshot_requests_never_call_scraper(self):
        self.service.runner = Mock(side_effect=AssertionError("unexpected scrape"))
        for body in [{"region": "US-XX"}, {"region": "CA"}, {"region": None}, {}, [], b"not json"]:
            self.assertEqual(self.request("POST", "/v1/snapshots", body)[0], 400)


if __name__ == "__main__":
    unittest.main()
