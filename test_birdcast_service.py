import json
import io
import hashlib
import os
import sys
import threading
import unittest
import http.client
import tempfile
import textwrap
import wave
from unittest.mock import MagicMock, patch
from http.server import ThreadingHTTPServer
from pathlib import Path
from birdcast_service import (BusyError, RecordingError, Service, SUPPORTED_REGIONS,
                              make_handler, RECORDING_BYTES, detect_system_services_status)


class ServiceTests(unittest.TestCase):
    def test_telemetry_lamp_snapshot_is_passive_and_keeps_controller_state(self):
        self.service.direction_alignment = 51
        self.service.direction_offset = 17
        lighting = {"brightness": 80, "target_brightness": 90, "color": "#112233",
                    "status": "error", "error": "disconnected", "radar_snapshot": {"private": True}}
        with patch.object(self.service.lighting, "status", return_value=lighting), \
                patch.object(self.service.presence_sensor, "response") as radar, \
                patch.object(self.service.light_sensor, "read") as light:
            snapshot = self.service.lamp_telemetry()
        self.assertEqual(snapshot["directionality"]["angle_degrees"], 120)
        self.assertEqual(snapshot["lighting"]["brightness"], 80)
        self.assertEqual(snapshot["lighting"]["target_brightness"], 90)
        self.assertEqual(snapshot["lighting"]["color"], "#112233")
        self.assertEqual(snapshot["lighting"]["error"], "disconnected")
        self.assertNotIn("radar_snapshot", snapshot["lighting"])
        radar.assert_not_called()
        light.assert_not_called()

    def setUp(self):
        self.snapshot = {"status": "ok", "region": {"code": "US-CA"}, "metrics": {"birds_in_flight": None}, "warnings": ["missing"]}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.service = Service(self.root, self.root / "config.json", runner=lambda region: self.snapshot)

    def test_wled_log_endpoint_is_passive_and_validates_cursor(self):
        handler = object.__new__(make_handler(self.service))
        handler._send = MagicMock()
        self.service.wled.log.append('received', 'boot')
        with patch.object(self.service.wled, 'send') as send:
            handler.path = '/v1/wled-log'
            handler.do_GET()
            status, data = handler._send.call_args.args
            self.assertEqual(status, 200)
            self.assertEqual(data['events'][0]['payload'], 'boot')
            handler.path = '/v1/wled-log?after=1'
            handler.do_GET()
            self.assertEqual(handler._send.call_args.args[1]['events'], [])
            send.assert_not_called()
        for query in ('after=-1', 'after=', 'after=abc', 'after=1&after=2',
                      'after=99999999999999999999999999', 'unknown=1'):
            handler.path = '/v1/wled-log?' + query
            handler.do_GET()
            self.assertEqual(handler._send.call_args.args[0], 400, query)

    def test_request_body_rejects_disconnect_and_timeout(self):
        handler = object.__new__(make_handler(self.service))
        self.assertEqual(handler.timeout, 10)
        handler.headers = {"Content-Length": "20"}
        handler.rfile = io.BytesIO(b'{"mode":"off"}')
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            handler._body()
        handler.rfile = MagicMock()
        handler.rfile.read.side_effect = TimeoutError()
        with self.assertRaisesRegex(ValueError, "Timed out"):
            handler._body()

    def test_usb_lighting_settings_handler_without_sockets(self):
        handler = object.__new__(make_handler(self.service))
        handler.path = '/v1/lighting'
        handler._send = MagicMock()
        self.service.lighting.transport = MagicMock()
        self.service.lighting._background = False
        self.service.set_station_mode('manual_control')
        handler._body = MagicMock(return_value={'brightness': 80, 'color': '#112233'})
        handler.do_POST()
        status, payload = handler._send.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(payload['lighting']['mode'], 'on')
        params = self.service.lighting.transport.send.call_args.args[0]
        self.assertEqual(params['bri'], 80)
        self.assertEqual(params['seg'][0]['col'], [[17, 34, 51, 0]])
        for invalid in ({}, {'color': None}, {'brightness': True}, {'brightness': 256}, {'unknown': 1}):
            handler._body.return_value = invalid
            handler.do_POST()
            self.assertEqual(handler._send.call_args.args[0], 400)

    def test_distance_mode_handler_and_passive_status(self):
        handler = object.__new__(make_handler(self.service))
        handler.path = '/v1/lighting'
        handler._send = MagicMock()
        handler._body = MagicMock(return_value={"mode": "distance_test"})
        self.service.lighting.transport = MagicMock()
        self.service.lighting._background = False
        handler.do_POST()
        self.assertEqual(handler._send.call_args.args[0], 200)
        self.assertEqual(handler._send.call_args.args[1]["lighting"]["mode"], "distance_test")
        self.service.lighting.update_distance(200, {"fresh": True, "presence": "moving", "moving_distance_cm": 600, "range_estimate_cm": 600})
        self.service.presence_sensor.response = MagicMock(return_value=(200, {"fresh": True}))
        latest = self.service.latest()
        self.assertEqual(latest["lighting"]["led_index"], 143)
        self.assertEqual(latest["sources"]["radar"]["result"]["range_estimate_cm"],
                         latest["lighting"]["distance_cm"])
        self.service.presence_sensor.response.assert_not_called()
        self.assertEqual(self.service.reading_history.recent("radar"), [])
        self.service.lighting.stop()

    def test_auto_uses_sampled_radar_and_color_handler_preserves_mode(self):
        light = self.service.lighting
        light._sound = {"source": "human"}
        light.transport = MagicMock()
        light._background = False
        light.set_mode("auto")
        light.update(0)
        light.update_distance(200, {"fresh": True, "presence": "moving", "moving_distance_cm": 350})
        light.update_distance(200, {"fresh": True, "presence": "stationary", "range_estimate_cm": 350, "stationary_distance_cm": 350})
        self.service.presence_sensor.response = MagicMock()
        latest = self.service.latest()
        self.assertEqual(latest["lighting"]["target_brightness"], 5)
        self.assertEqual(latest["sources"]["radar"]["result"]["range_estimate_cm"],
                         latest["lighting"]["distance_cm"])
        self.service.presence_sensor.response.assert_not_called()
        handler = object.__new__(make_handler(self.service))
        handler.path = "/v1/lighting"
        handler._send = MagicMock()
        self.service.set_station_mode("manual_control")
        handler._body = MagicMock(return_value={"color": "#112233"})
        handler.do_POST()
        self.assertEqual(handler._send.call_args.args[1]["lighting"]["mode"], "auto")
        self.assertEqual(light.status()["brightness"], 5)
        light.stop()

    def test_lux_readings_gate_auto_and_status_never_resamples(self):
        light = self.service.lighting
        light._sound = {"source": "human"}
        light._background = False
        light.transport = MagicMock()
        self.service.light_sensor.response = MagicMock(return_value=(200, {'lux': 9}))
        self.service.light_reading()
        light.update_distance(200, {'fresh': True, 'presence': 'moving', 'range_estimate_cm': 350, 'moving_distance_cm': 350})
        self.assertEqual(light.status()['brightness'], 5)
        self.service.light_sensor.response.return_value = (200, {'lux': 31})
        self.service.light_reading()
        state = self.service.latest()['lighting']
        self.assertEqual(state['brightness'], 0)
        self.assertEqual(state['auto_gate_reason'], 'daylight')
        self.assertEqual(state['lux'], 31)
        self.assertEqual(state['lux_dark_threshold'], 15)
        self.assertEqual(state['lux_light_threshold'], 25)
        count = light.transport.send.call_count
        self.service.latest()
        self.assertEqual(light.transport.send.call_count, count)
        self.assertEqual(self.service.light_sensor.response.call_count, 2)
        self.service.light_sensor.response.return_value = (503, {'error': 'disconnected'})
        self.service.light_reading()
        self.assertEqual(light.status()['auto_gate_reason'], 'lux_unavailable')
        self.assertEqual(light.status()['brightness'], 0)
        self.assertEqual(len(self.service.reading_history.recent('light')), 3)
        self.assertEqual(self.service.reading_history.recent('light')[-1]['http_status'], 503)

    def test_birdcast_snapshot_updates_lighting_cache_without_polling_provider(self):
        self.service.lighting = MagicMock()
        result = self.service.snapshot("US-CA")
        self.service.lighting.update_migration.assert_called_once_with(result)
        self.service.lighting.update_migration.reset_mock()
        self.service.remember("birdcast", 503, {"error": "offline"})
        self.service.lighting.update_migration.assert_called_once_with(None)

    def test_lux_schedule_is_separate_from_presence_and_keeps_passive_reads(self):
        scheduler = self.service.collection_schedule
        self.assertEqual(scheduler.sensor_interval, 10)
        self.assertEqual(scheduler.light_interval, 1)
        self.assertEqual(scheduler.collect_sensors, self.service.presence_reading)
        self.assertEqual(scheduler.collect_light, self.service.light_reading)
        self.service.lighting = MagicMock()
        self.service.light_sensor.response = MagicMock(return_value=(200, {'lux': 5}))
        self.service.presence_sensor.response = MagicMock(return_value=(503, {'fresh': False}))
        scheduler.collect_sensors()
        for _ in range(3):
            scheduler.collect_light()
        self.assertEqual(len(self.service.reading_history.recent('light')), 3)
        self.assertEqual(len(self.service.reading_history.recent('radar')), 1)
        self.service.lighting.status.return_value = {}
        self.service.latest()
        self.assertEqual(self.service.light_sensor.response.call_count, 3)
        self.assertEqual(self.service.presence_sensor.response.call_count, 2)
        # latest() may read passive radar state outside lighting Auto.
        self.assertEqual(len(self.service.reading_history.recent('radar')), 1)
        self.assertEqual(self.service.latest()['collection']['light_interval_seconds'], 1)

    def test_lux_collection_and_latest_are_passive(self):
        self.service.lighting = MagicMock()
        self.service.light_sensor.response = MagicMock(return_value=(200, {"lux": 12, "ambient_counts": 24, "white_counts": 36}))
        self.service.light_reading()
        self.service.lighting.update.assert_called_once_with(12)
        self.service.lighting.status.return_value = {"brightness": 173}
        latest = self.service.latest()
        self.assertEqual(latest["lighting"]["brightness"], 173)
        self.assertEqual(latest["sources"]["light"]["result"]["ambient_counts"], 24)
        self.assertEqual(self.service.reading_history.recent("light")[-1]["result"]["white_counts"], 36)
        self.service.light_sensor.response.assert_called_once()
        self.service.light_sensor.response.return_value = (503, {"error": "unavailable"})
        self.service.light_reading()
        self.service.lighting.update.assert_called_with(None)

    def test_manual_lighting_endpoint_validation_and_failure(self):
        self.service.set_station_mode("manual_control")
        self.service.lighting = MagicMock()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            for mode, state, expected in (("on", "ok", 200), ("off", "controller_unavailable", 200)):
                self.service.lighting.configure.return_value = {"mode": mode, "status": state}
                conn.request("POST", "/v1/lighting", json.dumps({"mode": mode}), {"Content-Type": "application/json"})
                response = conn.getresponse()
                self.assertEqual(response.status, expected)
                self.assertEqual(json.loads(response.read())["lighting"]["mode"], mode)
                self.service.lighting.configure.assert_called_with(mode=mode)
            conn.request("POST", "/v1/lighting", '{}', {"Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            conn.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_disconnected_clients_close_without_hiding_other_errors(self):
        handler_class = make_handler(self.service)
        for response in ("json", "bytes"):
            for failure in (BrokenPipeError(), ConnectionResetError()):
                handler = handler_class.__new__(handler_class)
                handler.send_response = MagicMock()
                handler.send_header = MagicMock()
                handler.end_headers = MagicMock()
                handler.wfile = MagicMock()
                handler.wfile.write.side_effect = failure
                def send(instance):
                    if response == "json":
                        instance._send(200, {"status": "ok"})
                    else:
                        instance._send_bytes(b"audio", "audio/wav")
                with patch("birdcast_service.BaseHTTPRequestHandler.handle", send):
                    handler.handle()
                self.assertTrue(handler.close_connection)
        with patch("birdcast_service.BaseHTTPRequestHandler.handle", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                handler.handle()

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

    def test_recording_retention_boundary_and_busy_retry(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 11, 8, tzinfo=timezone.utc)
        entries = [{"recording_id": name, "created_at": (now - timedelta(days=days)).isoformat()}
                   for name, days in (("old", 6), ("busy", 7), ("boundary", 5), ("new", 1))]
        with patch.object(self.service.recording_store, "list_recordings", return_value=entries), \
                patch.object(self.service, "delete_recording", side_effect=[None, BusyError("active")]) as delete:
            self.assertEqual(self.service.expire_recordings(now), 1)
            self.assertEqual([call.args[0] for call in delete.call_args_list], ["old", "busy"])
        with patch.object(self.service.recording_store, "list_recordings", return_value=entries[1:2]), \
                patch.object(self.service, "delete_recording") as delete:
            self.assertEqual(self.service.expire_recordings(now), 1)
            delete.assert_called_once_with("busy")

    def test_expiration_removes_recording_analysis_history_and_cache(self):
        from datetime import datetime, timedelta, timezone
        self.service.recorder = self.recorder_script(self.root)
        metadata = self.service.start_recording()
        identifier = metadata["recording_id"]
        self.assertTrue(self.service.recording_store.path(identifier).exists())
        future = datetime.fromisoformat(metadata["created_at"].replace("Z", "+00:00")) + timedelta(days=6)
        self.assertEqual(self.service.expire_recordings(future), 1)
        self.assertFalse(self.service.recording_store.path(identifier).exists())
        self.assertFalse(self.service.recording_store.path(identifier).with_suffix(".json").exists())
        self.assertFalse((self.service.animal_analysis.directory / (identifier + ".json")).exists())
        self.assertEqual(self.service.reading_history.recent("microphone"), [])
        self.assertNotIn("microphone", self.service._latest)

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
            self.assertEqual(metadata["frame_count"], 240000)
            self.assertEqual(metadata["byte_length"], 44 + RECORDING_BYTES)
            details, data = service.recording(metadata["recording_id"])
            self.assertEqual(len(data), details["byte_length"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), details["sha256"])
            service.finish_transfer()
            self.assertEqual(service.recording(metadata["recording_id"])[1], data)
            service.delete_recording(metadata["recording_id"])
            with self.assertRaises(RecordingError):
                service.recording(metadata["recording_id"])
            self.assertIn("analysis", metadata)

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
                self.assertEqual(json.loads(response.read())["deleted"], True)
                conn.request("POST", "/v1/snapshots", json.dumps({"region": "US-CA"}), {"Content-Type": "application/json"})
                response = conn.getresponse(); self.assertEqual(response.status, 200)
            finally:
                server.shutdown(); thread.join(); server.server_close()

    def test_station_mode_defaults_to_normal_and_can_be_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), Path(directory) / "config.json")
            self.assertEqual(service.mode, "normal")
            self.assertEqual(service.latest()["mode"], "normal")
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request("GET", "/v1/station-mode")
                res = conn.getresponse(); self.assertEqual(res.status, 200)
                body = json.loads(res.read())
                self.assertEqual(body["mode"], "normal")
                self.assertIn("direction_config", body["modes"])
                self.assertIn("manual_control", body["modes"])

                conn.request("POST", "/v1/station-mode", json.dumps({"mode": "direction_config"}), {"Content-Type": "application/json"})
                res = conn.getresponse(); self.assertEqual(res.status, 200)
                body = json.loads(res.read())
                self.assertEqual(body["mode"], "direction_config")
                self.assertEqual(service.mode, "direction_config")
                self.assertEqual(service.latest()["mode"], "direction_config")

                conn.request("POST", "/v1/station-mode", json.dumps({"mode": "manual_control"}), {"Content-Type": "application/json"})
                res = conn.getresponse(); self.assertEqual(res.status, 200)
                body = json.loads(res.read())
                self.assertEqual(body["mode"], "manual_control")
                self.assertEqual(service.mode, "manual_control")
                self.assertEqual(service.latest()["mode"], "manual_control")

                conn.request("POST", "/v1/station-mode", json.dumps({"mode": "invalid_mode"}), {"Content-Type": "application/json"})
                res = conn.getresponse(); self.assertEqual(res.status, 400)
            finally:
                server.shutdown(); thread.join(); server.server_close()

    def test_direction_alignment_endpoint_and_mode_gating(self):
        with tempfile.TemporaryDirectory() as directory:
            service = Service(Path(directory), Path(directory) / "config.json")
            service.lighting._background = False
            service.lighting.transport = MagicMock()
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                # GET initial alignment and beam width
                conn.request("GET", "/v1/direction/alignment")
                res = conn.getresponse(); self.assertEqual(res.status, 200)
                body = json.loads(res.read())
                self.assertEqual(body["alignment"], 144)
                self.assertEqual(body["beam_width"], 1)
                self.assertEqual(body["max_beam_width"], 143)
                self.assertEqual(body["mode"], "normal")

                # POST while in normal mode is rejected with 409
                conn.request("POST", "/v1/direction/alignment", json.dumps({"alignment": 72}), {"Content-Type": "application/json"})
                res = conn.getresponse(); self.assertEqual(res.status, 409)
                body = json.loads(res.read())
                self.assertEqual(body["error"]["code"], "invalid_mode")

                # Switch to direction_config
                service.set_station_mode("direction_config")

                # POST with valid alignment and beam_width succeeds
                conn.request("POST", "/v1/direction/alignment", json.dumps({"alignment": 10, "beam_width": 3}), {"Content-Type": "application/json"})
                res = conn.getresponse(); self.assertEqual(res.status, 200)
                body = json.loads(res.read())
                self.assertEqual(body["alignment"], 10)
                self.assertEqual(body["beam_width"], 3)
                self.assertEqual(body["max_beam_width"], 11)
                self.assertEqual(service.direction_alignment, 10)
                self.assertEqual(service.direction_beam_width, 3)

                # POST beam_width only
                conn.request("POST", "/v1/direction/alignment", json.dumps({"beam_width": 5}), {"Content-Type": "application/json"})
                res = conn.getresponse(); self.assertEqual(res.status, 200)
                body = json.loads(res.read())
                self.assertEqual(body["beam_width"], 5)
                self.assertEqual(service.direction_beam_width, 5)

                # POST offset on /v1/direction
                conn.request("POST", "/v1/direction", json.dumps({"offset": 3}), {"Content-Type": "application/json"})
                res = conn.getresponse(); self.assertEqual(res.status, 200)
                body = json.loads(res.read())
                self.assertEqual(body["offset"], 3)
                self.assertEqual(service.direction_offset, 3)
                self.assertEqual(body["angle_degrees"], 108.0)

                # POST angle on /v1/direction
                conn.request("POST", "/v1/direction", json.dumps({"angle": 180}), {"Content-Type": "application/json"})
                res = conn.getresponse(); self.assertEqual(res.status, 200)
                body = json.loads(res.read())
                self.assertEqual(body["offset"], 5)
                self.assertEqual(service.direction_offset, 5)

                # GET /v1/direction returns state
                conn.request("GET", "/v1/direction")
                res = conn.getresponse(); self.assertEqual(res.status, 200)
                body = json.loads(res.read())
                self.assertEqual(body["offset"], 5)
                self.assertEqual(body["angle_degrees"], 180.0)

                # Latest reflects alignment, beam width, and offset
                self.assertEqual(service.latest()["directionality"]["alignment"], 10)
                self.assertEqual(service.latest()["directionality"]["beam_width"], 5)
                self.assertEqual(service.latest()["directionality"]["offset"], 5)
                self.assertEqual(service.latest()["directionality"]["max_beam_width"], 11)

                # Bounds checks for alignment
                for invalid in (0, 145, "invalid"):
                    conn.request("POST", "/v1/direction/alignment", json.dumps({"alignment": invalid}), {"Content-Type": "application/json"})
                    res = conn.getresponse(); self.assertEqual(res.status, 400)

                # Bounds checks for offset
                for invalid_off in (-1, 10, "five"):
                    conn.request("POST", "/v1/direction", json.dumps({"offset": invalid_off}), {"Content-Type": "application/json"})
                    res = conn.getresponse(); self.assertEqual(res.status, 400)

                # Bounds checks for beam_width (even number, zero, negative, exceeding max)
                for invalid_bw in (0, 2, 4, 13, -1, "three"):
                    conn.request("POST", "/v1/direction/alignment", json.dumps({"beam_width": invalid_bw}), {"Content-Type": "application/json"})
                    res = conn.getresponse(); self.assertEqual(res.status, 400)
            finally:
                server.shutdown(); thread.join(); server.server_close()

    def test_system_services_status_and_latest_broadcast(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.json"
            service = Service(root, config, animal_analysis=False)
            status = service.get_services_status()
            self.assertIn("bioglow_service", status)
            self.assertIn("cloudflare_tunnel", status)
            self.assertIsInstance(status["bioglow_service"]["installed"], bool)
            self.assertIsInstance(status["bioglow_service"]["started"], bool)
            self.assertIsInstance(status["cloudflare_tunnel"]["set_up"], bool)

            latest = service.latest()
            self.assertIn("services", latest)
            self.assertEqual(latest["services"], status)

            # Test detect_system_services_status with simulated paths
            mock_bg = root / "mock_bioglow.service"
            mock_bg.write_text("[Unit]\nDescription=Test\n")
            mock_cf = root / "mock_cloudflared.service"
            mock_cf.write_text("[Unit]\nDescription=CF\n")
            mock_token = root / "mock_token"
            mock_token.write_text("dummy-token-content")

            custom_status = detect_system_services_status(
                bioglow_unit=str(mock_bg),
                cloudflared_unit=str(mock_cf),
                cloudflared_token=str(mock_token),
            )
            self.assertTrue(custom_status["bioglow_service"]["installed"])
            self.assertTrue(custom_status["cloudflare_tunnel"]["installed"])
            self.assertTrue(custom_status["cloudflare_tunnel"]["token_configured"])
            self.assertTrue(custom_status["cloudflare_tunnel"]["set_up"])

if __name__ == "__main__":
    unittest.main()
