import unittest
import json
import threading
import socket
from unittest.mock import MagicMock, patch

from lighting_control import LightingControl, distance_brightness


class LightingTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.opener = MagicMock()
        self.opener.return_value.__enter__.return_value.status = 200
        self.light = LightingControl(opener=self.opener, clock=lambda: self.now)

    def read(self, value):
        self.now += 31
        self.light.update(value)
        return self.light.status()

    def test_background_controller_does_not_block_sensor_or_status(self):
        entered, release = threading.Event(), threading.Event()
        def slow_controller(*args, **kwargs):
            entered.set()
            release.wait(2)
            raise OSError('offline')
        light = LightingControl(opener=slow_controller, background=True)
        try:
            with patch('lighting_control.socket.gethostbyname', return_value='192.0.2.1'):
                light.set_mode('auto')
                self.assertTrue(entered.wait(1))
                self.assertEqual(light.status()['status'], 'pending')
                light.set_mode('off')
                self.assertEqual(light.status()['target_brightness'], 0)
                light.update(500)
                self.assertEqual(light.status()['mode'], 'off')
                release.set()
        finally:
            release.set()
            light.stop()

    def test_manual_command_retries_latest_state_after_connection_failure(self):
        failed, recovered = threading.Event(), threading.Event()
        light = LightingControl(background=True)
        self.addCleanup(light.stop)
        calls = []
        def request(target, *args):
            calls.append(target)
            if len(calls) == 1:
                failed.set()
                return 'USB disconnected'
            recovered.set()
            return None
        light._request = request
        light.set_mode('on')
        self.assertTrue(failed.wait(1))
        self.assertTrue(recovered.wait(4))
        with light._condition:
            self.assertTrue(light._condition.wait_for(lambda: light._state['status'] == 'ok', timeout=1))
        self.assertEqual(calls, [255, 255])

    def test_mdns_fallback(self):
        light = LightingControl(opener=self.opener, background=True)
        with patch('lighting_control.socket.gethostbyname', side_effect=socket.gaierror('DNS')), patch('lighting_control.subprocess.run') as resolve:
            resolve.return_value.stdout = json.dumps({"type": "iisiu", "data": [2, 0, 'bioglow.local', 0, '192.0.2.1', 0]})
            self.assertIsNone(light._request(0))
            self.assertIn('192.0.2.1/json/state', self.opener.call_args.args[0].full_url)
            self.assertIsNone(light._request(0))
            self.assertEqual(resolve.call_count, 1)

    def test_missing_resolver_reports_actionable_connection_error(self):
        light = LightingControl(opener=self.opener, background=True)
        with patch('lighting_control.socket.gethostbyname', side_effect=socket.gaierror('DNS')), patch('lighting_control.subprocess.run', side_effect=FileNotFoundError('busctl')):
            error = light._request(0)
            self.assertIn('BIOGLOW_LIGHT_HOST', error)
            self.assertNotIn('No such file', error)
            self.opener.assert_not_called()

    def test_invalid_resolver_reply_does_not_send_request(self):
        light = LightingControl(opener=self.opener, background=True)
        with patch('lighting_control.socket.gethostbyname', side_effect=socket.gaierror('DNS')), patch('lighting_control.subprocess.run') as resolve:
            resolve.return_value.stdout = '{"data": [2, 0, "bioglow.local", 0, "invalid", 0]}'
            self.assertIn('Cannot resolve', light._request(0))
            self.opener.assert_not_called()

    def test_manual_color_brightness_survive_sampling_and_power_cycle(self):
        usb = MagicMock()
        light = LightingControl(transport=usb)
        state = light.configure(brightness=80, color='#00FF44')
        self.assertEqual(state['mode'], 'on')
        self.assertEqual(state['brightness'], 80)
        params = usb.send.call_args.args[0]
        self.assertEqual(params['seg'][0]['col'], [[0, 255, 68, 0]])
        light.update(600)
        self.assertEqual(light.status()['brightness'], 80)
        light.update(None)
        self.assertEqual(light.status()['brightness'], 80)
        light.set_mode('off')
        light.configure(color='#001122', brightness=42)
        self.assertFalse(light.status()['on'])
        light.set_mode('on')
        self.assertEqual(light.status()['brightness'], 42)
        light.set_mode('auto')
        self.assertEqual(light.status()['mode'], 'auto')
        light.update(600)
        self.assertEqual(light.status()['brightness'], 0)

    def test_invalid_manual_settings_are_atomic(self):
        for settings in ({'brightness':True}, {'brightness':0}, {'brightness':256},
                         {'brightness':2.5}, {'color':'red'}, {'color':'#aabbccdd'},
                         {'brightness':80, 'color':'invalid'}, {'mode':False}):
            before = self.light.status()
            with self.assertRaises(ValueError):
                self.light.configure(**settings)
            self.assertEqual(self.light.status(), before)
        self.opener.assert_not_called()

    def test_distance_brightness_endpoints_and_clamping(self):
        self.light.update(0)
        for distance, target in ((1,38), (100,38), (350,83), (600,128), (900,128)):
            self.assertEqual(distance_brightness(distance), target)
            self.light.update_distance(200, dict(fresh=True, presence='moving', range_estimate_cm=distance))
            self.assertEqual(self.light.status()['brightness'], target)

    def test_unavailable_radar_turns_off_immediately(self):
        self.light.update(0)
        for reading in ({'fresh':False}, {'fresh':True, 'presence':'none'},
                        {'fresh':True, 'presence':'moving', 'range_estimate_cm':None},
                        {'fresh':True, 'presence':'moving', 'range_estimate_cm':350, 'age_seconds':3}):
            self.light.update_distance(200, dict(fresh=True, presence='moving', range_estimate_cm=600))
            self.light.update_distance(200, reading)
            self.assertEqual(self.light.status()['brightness'], 0)
            self.assertEqual(json.loads(self.opener.call_args.args[0].data)['transition'], 0)

    def test_manual_override_survives_reads_and_auto_restores_lux_control(self):
        self.read(500)
        self.assertEqual(self.light.set_mode('on')['brightness'], 255)
        self.assertEqual(self.read(600)['brightness'], 255)
        self.assertEqual(self.light.set_mode('auto')['brightness'], 0)
        self.read(0)
        self.assertEqual(self.light.set_mode('off')['brightness'], 0)
        self.assertEqual(self.read(0)['brightness'], 0)
        self.assertEqual(self.light.set_mode('auto')['brightness'], 0)

    def test_manual_works_without_sensor_and_rejects_invalid_modes(self):
        self.assertEqual(self.light.set_mode('on')['brightness'], 255)
        self.assertEqual(self.read(None)['brightness'], 255)
        self.assertEqual(self.light.set_mode('auto')['brightness'], 0)
        with self.assertRaises(ValueError):
            self.light.set_mode('invalid')

    def test_failure_retries_without_claiming_success(self):
        self.light.update(0)
        self.opener.side_effect = OSError('offline')
        self.light.update_distance(200, dict(fresh=True, presence='moving', range_estimate_cm=600))
        self.assertIsNone(self.light.status()['brightness'])
        self.assertEqual(self.light.status()['target_brightness'], 128)
        self.light.update_distance(200, dict(fresh=True, presence='moving', range_estimate_cm=600))
        self.assertEqual(self.opener.call_count, 2)

    def test_smoothing_and_wled_command(self):
        self.light.set_mode('auto')
        self.light.update(600)
        self.now = 10
        self.light.update(0)
        self.assertEqual(self.light.status()['smoothed_lux'], 300)
        self.assertEqual(self.light.status()['brightness'], 0)
        request = self.opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertFalse(payload['on'])
        self.assertEqual(payload['transition'], 0)
        self.assertEqual(payload['lor'], 2)
        self.assertIn('bioglow.local/json/state', request.full_url)


class DistanceLightingTests(unittest.TestCase):
    def setUp(self):
        self.transport = MagicMock()
        self.transport.send.return_value = {}
        self.now = 0
        self.light = LightingControl(transport=self.transport, clock=lambda: self.now)
        self.light.set_mode("distance_test")
        self.addCleanup(self.light.stop)

    def reading(self, distance=100, presence="moving", **extra):
        return dict(fresh=True, presence=presence, range_estimate_cm=distance, moving_distance_cm=distance,
                    stationary_distance_cm=distance, **extra)

    def test_mapping_and_fixed_appearance(self):
        for distance, index in ((1, 0), (99, 0), (100, 0), (101, 1), (350, 72), (599, 142), (600, 143), (700, 143)):
            with self.subTest(distance=distance):
                self.light.update_distance(200, self.reading(distance))
                state = self.light.status()
                self.assertEqual(state["led_index"], index)
                command = self.transport.send.call_args.args[0]
                self.assertEqual(command["bri"], 64)
                self.assertEqual(command["transition"], 0)
                self.assertEqual(command["seg"][0]["col"], [[255, 160, 40, 0]])
                self.assertEqual((command["seg"][0]["start"], command["seg"][0]["stop"]), (index, index+1))

    def test_target_selection_uses_filtered_range_not_raw_channels(self):
        for presence, expected in (("moving", 350), ("stationary", 350), ("both", 350)):
            self.light.update_distance(200, dict(fresh=True, presence=presence, range_estimate_cm=350,
                                               moving_distance_cm=600, stationary_distance_cm=200))
            self.assertEqual(self.light.status()["distance_cm"], expected)

    def test_no_target_errors_and_recovery(self):
        self.light.update_distance(200, self.reading(presence="none"))
        self.assertEqual(self.light.status()["test_state"], "no_target")
        self.assertNotIn("sensor_error", self.light.status())
        for status, reading in ((503, {}), (200, {"fresh": False}), (200, self.reading(None))):
            self.light.update_distance(status, reading)
            state = self.light.status()
            self.assertEqual(state["test_state"], "sensor_unavailable")
            self.assertIn("sensor_error", state)
            self.assertFalse(self.transport.send.call_args.args[0]["on"])
        self.light.update_distance(200, self.reading())
        self.assertEqual(self.light.status()["test_state"], "tracking")
        self.assertNotIn("sensor_error", self.light.status())

    def test_lux_and_obsolete_radar_cannot_override_mode(self):
        self.transport.reset_mock()
        self.light.update(0)
        self.assertFalse(self.transport.send.called)
        revision = self.light._revision
        self.light.set_mode("off")
        self.transport.reset_mock()
        self.light.update_distance(200, self.reading(), revision)
        self.assertFalse(self.transport.send.called)

    def test_appearance_retained_and_normal_coverage_restored(self):
        with self.assertRaises(ValueError):
            self.light.configure(brightness=100)
        self.light.update_distance(200, self.reading())
        self.light.set_mode("on")
        command = self.transport.send.call_args.args[0]
        self.assertNotIn("start", command["seg"][0])
        self.assertEqual(command["bri"], 255)
        self.assertNotIn("led_index", self.light.status())

    def test_auto_waits_for_new_radar_after_test(self):
        self.light.update_distance(200, self.reading())
        self.light.update(0)
        self.light.set_mode("auto")
        command = self.transport.send.call_args.args[0]
        self.assertEqual(command["bri"], 0)
        self.assertNotIn("start", command["seg"][0])

    def test_controller_error_separate_from_sensor_error(self):
        self.light.update_distance(200, self.reading(350))
        self.transport.send.side_effect = OSError("USB disconnected")
        self.light.update_distance(503, {})
        state = self.light.status()
        self.assertEqual(state["status"], "controller_unavailable")
        self.assertIn("sensor_error", state)
        self.assertIn("USB disconnected", state["error"])

    def test_unchanged_output_is_deduplicated_and_periodically_reasserted(self):
        self.light.update_distance(200, self.reading(350))
        sent = self.transport.send.call_count
        for _ in range(3):
            self.now += 0.5
            self.light.update_distance(200, self.reading(350))
        self.assertEqual(self.transport.send.call_count, sent)
        self.now += 0.5
        self.light.update_distance(200, self.reading(350))
        self.assertEqual(self.transport.send.call_count, sent + 1)
        self.light.update_distance(200, self.reading(presence="none"))
        self.assertFalse(self.transport.send.call_args.args[0]['on'])

    def test_snapshot_is_atomic_copied_and_expires(self):
        reading = self.reading(350, timestamp="sample-1", age_seconds=0.1, stale_after_seconds=2)
        self.light.update_distance(200, reading)
        reading["range_estimate_cm"] = 650
        status = self.light.status()
        self.assertEqual(status['distance_cm'], status['radar_snapshot']['result']['range_estimate_cm'])
        self.assertEqual(status['confirmed_led_index'], status['led_index'])
        self.assertEqual(status['confirmed_radar_timestamp'], 'sample-1')
        status['radar_snapshot']['result']['range_estimate_cm'] = 700
        self.assertEqual(self.light.status()['distance_cm'], 350)
        self.now += 3
        status = self.light.status()
        self.assertEqual(status['radar_snapshot']['http_status'], 503)
        self.assertIsNone(status['distance_cm'])
        self.assertIsNone(status['led_index'])
        self.assertEqual(status['confirmed_distance_cm'], 350, 'Keep last acknowledged output explicitly historical')

    def test_filtered_null_does_not_fall_back_to_raw_distance(self):
        self.light.update_distance(200, self.reading(350))
        self.light.update_distance(200, dict(fresh=True, presence='moving', range_estimate_cm=None,
                                           moving_distance_cm=650))
        self.assertIsNone(self.light.status()['led_index'])
        self.assertFalse(self.transport.send.call_args.args[0]['on'])

    def test_delayed_acknowledgment_keeps_target_and_confirmed_separate(self):
        entered, release, final_ack = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def send(command):
            calls.append(command)
            if len(calls) == 2:
                entered.set()
                release.wait(2)
            if len(calls) == 3:
                final_ack.set()
            return {}
        transport = MagicMock()
        transport.send.side_effect = send
        light = LightingControl(transport=transport, background=True)
        try:
            light.set_mode('distance_test')
            # Wait for the initial off command to finish without a hardware sleep.
            with light._condition:
                light._condition.wait_for(lambda: light._state['status'] == 'ok', timeout=1)
            light.update_distance(200, self.reading(350, timestamp='near'))
            self.assertTrue(entered.wait(1))
            light.update_distance(200, self.reading(650, timestamp='far'))
            status = light.status()
            self.assertEqual(status['led_index'], 143)
            self.assertIsNone(status['confirmed_led_index'])
            release.set()
            self.assertTrue(final_ack.wait(2))
        finally:
            release.set()
            light.stop()
        self.assertEqual(calls[2]['seg'][0]['start'], 143)

    def test_sampling_interval_and_shutdown_off(self):
        import time
        self.light._clock = time.monotonic
        sampled = threading.Event()
        times = []
        def read():
            times.append(time.monotonic())
            if len(times) >= 3:
                sampled.set()
            return 200, self.reading()
        self.light.start_distance_sampling(read)
        self.assertTrue(sampled.wait(2))
        self.light.stop()
        self.assertTrue(all(0.45 <= b-a < 0.9 for a, b in zip(times, times[1:])))
        self.assertFalse(self.transport.send.call_args.args[0]["on"])
        self.transport.close.assert_called()

    def test_inflight_command_cannot_override_newer_mode_status(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        commands = []
        def send(command):
            commands.append(command)
            if len(commands) == 1:
                entered.set()
                release.wait(2)
            else:
                finished.set()
            return {}
        transport = MagicMock()
        transport.send.side_effect = send
        light = LightingControl(transport=transport, background=True)
        try:
            light.set_mode("distance_test")
            self.assertTrue(entered.wait(1))
            light.update_distance(200, self.reading())
            light.set_mode("on")
            release.set()
            self.assertTrue(finished.wait(2))
        finally:
            release.set()
            light.stop()
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[-1]["bri"], 255)
        self.assertNotIn("start", commands[-1]["seg"][0])
        self.assertEqual(light.status()["mode"], "on")


class AutoLightingTests(unittest.TestCase):
    def setUp(self):
        DistanceLightingTests.setUp(self)
        self.light.set_mode("auto")
        self.light.update(0)

    reading = DistanceLightingTests.reading
    def test_auto_color_and_full_strip(self):
        self.light.update_distance(200, self.reading(350))
        self.light.configure(color="#123456")
        state = self.light.status()
        self.assertEqual(state["mode"], "auto")
        self.assertEqual(state["brightness"], 83)
        command = self.transport.send.call_args.args[0]
        self.assertEqual(command["transition"], 3)
        self.assertNotIn("start", command["seg"][0])
        self.assertEqual(command["seg"][0]["col"], [[18,52,86,0]])
        self.light.set_mode("on")
        self.assertEqual(self.light.status()["brightness"], 255)

    def test_static_target_expires_and_only_distance_change_reactivates(self):
        self.light.update_distance(200, self.reading(350))
        self.assertEqual(self.light.status()["brightness"], 83)
        sent = self.transport.send.call_count
        self.light.update_distance(200, self.reading(352))
        self.assertEqual(self.light.status()["brightness"], 83)
        self.assertEqual(self.transport.send.call_count, sent)
        for tick in range(1, 10):
            self.now = tick * 0.5
            self.light.update_distance(200, self.reading(350))
        self.assertEqual(self.transport.send.call_count, sent)
        self.now = 5
        self.light.update_distance(200, self.reading(350))
        self.assertEqual(self.light.status()["brightness"], 0)
        self.assertEqual(self.light.status()["test_state"], "idle")
        sent = self.transport.send.call_count
        self.now = 20
        self.light.update_distance(200, self.reading(352))
        self.assertEqual(self.transport.send.call_count, sent, "Small range jitter must not wake lights")
        self.light.configure(color="#112233")
        self.assertEqual(self.light.status()["brightness"], 0)
        self.light.update_distance(200, self.reading(400))
        self.assertGreater(self.light.status()["brightness"], 0)
        self.assertLessEqual(self.light.status()["brightness"], 128)

    def test_auto_continuously_updates_without_dashboard_requests(self):
        transport = MagicMock()
        transport.send.return_value = {}
        light = LightingControl(transport=transport)
        light.update(0)
        readings = iter((100, 350, 600, None, 100))
        targets = []
        finished = threading.Event()
        def read():
            distance = next(readings, 100)
            return 200, self.reading(distance, presence="none" if distance is None else "moving")
        def send(command):
            # The initial startup command requests off before the first sample.
            if light._radar_snapshot is not None:
                targets.append(command["bri"])
                if len(targets) >= 5:
                    finished.set()
            return {}
        transport.send.side_effect = send
        try:
            light.start_distance_sampling(read)
            self.assertTrue(finished.wait(4))
            self.assertEqual(targets[:5], [38, 83, 128, 0, 38])
            self.assertEqual(light.status()["mode"], "auto")
        finally:
            light.stop()

    def test_mode_switch_clears_sample_time_and_keeps_worker_running(self):
        transport = MagicMock()
        transport.send.return_value = {}
        light = LightingControl(transport=transport, background=True)
        light.update(0)
        try:
            for mode in ("auto", "distance_test", "auto"):
                light.update_distance(200, self.reading(350))
                with light._condition:
                    self.assertTrue(light._condition.wait_for(
                        lambda: light._state["status"] == "ok", timeout=1))
                light.set_mode(mode)
                self.assertIsNone(light.status()["sample_age_seconds"])
                with light._condition:
                    self.assertTrue(light._condition.wait_for(
                        lambda: light._state["status"] == "ok", timeout=1))
                self.assertTrue(light._worker.is_alive())
                self.assertEqual(light.status()["brightness"], 0)
            light.update_distance(200, self.reading(600))
            with light._condition:
                self.assertTrue(light._condition.wait_for(
                    lambda: light._state["brightness"] == 128, timeout=1))
        finally:
            light.stop()

    def test_watchdog_tolerates_missing_snapshot_with_old_sample_time(self):
        transport = MagicMock()
        transport.send.return_value = {}
        light = LightingControl(transport=transport, background=True)
        light.update(0)
        try:
            # Submit while holding the lock so the first worker iteration
            # necessarily sees an old sample time and a missing snapshot.
            with light._condition:
                light._radar_sampled_at = 0
                light._send(128)
                self.assertTrue(light._condition.wait_for(
                    lambda: light._state["status"] == "ok", timeout=1))
            self.assertTrue(light._worker.is_alive())
        finally:
            light.stop()

    def test_output_retries_failure_without_another_sensor_submission(self):
        now = [0]
        transport = MagicMock()
        transport.send.side_effect = [OSError("disconnected"), {}]
        light = LightingControl(transport=transport, background=True, clock=lambda: now[0])
        light.update(0)
        try:
            light.update_distance(200, self.reading(350))
            with light._condition:
                self.assertTrue(light._condition.wait_for(
                    lambda: light._state["status"] == "controller_unavailable", timeout=1))
                now[0] = 0.6
                light._condition.notify_all()
                self.assertTrue(light._condition.wait_for(
                    lambda: light._state["brightness"] == 83, timeout=2))
            self.assertEqual(transport.send.call_count, 2)
            self.assertNotIn("error", light.status())
        finally:
            transport.send.side_effect = None
            light.stop()

    def test_output_watchdog_turns_off_when_sampling_stops(self):
        now = [0]
        transport = MagicMock()
        transport.send.return_value = {}
        light = LightingControl(transport=transport, background=True, clock=lambda: now[0])
        light.update(0)
        try:
            light.update_distance(200, self.reading(600))
            with light._condition:
                self.assertTrue(light._condition.wait_for(
                    lambda: light._state["brightness"] == 128, timeout=1))
                now[0] = 3
                light._condition.notify_all()
                self.assertTrue(light._condition.wait_for(
                    lambda: light._state["brightness"] == 0, timeout=2))
            self.assertIn("sampling stopped", light.status()["sensor_error"])
            self.assertEqual(transport.send.call_args.args[0]["transition"], 0)
            self.assertEqual(light.status()["sample_age_seconds"], 3)
        finally:
            light.stop()

    def test_malformed_sensor_frame_does_not_kill_sampling(self):
        recovered = threading.Event()
        transport = MagicMock()
        def send(command):
            if command["bri"] == 38:
                recovered.set()
            return {}
        transport.send.side_effect = send
        light = LightingControl(transport=transport, background=True)
        light.update(0)
        frames = iter((None, {"fresh":True, "age_seconds":"invalid"}, self.reading(100)))
        try:
            light.start_distance_sampling(lambda: (200, next(frames, self.reading(100))))
            self.assertTrue(recovered.wait(3))
            self.assertTrue(light.status()["radar_loop_running"])
            self.assertNotIn("sensor_error", light.status())
        finally:
            light.stop()

    def test_sampling_continues_while_controller_is_slow(self):
        entered, release, sampled = threading.Event(), threading.Event(), threading.Event()
        transport = MagicMock()
        calls = [0]
        def read():
            calls[0] += 1
            if calls[0] >= 3:
                sampled.set()
            return 200, self.reading(100 if calls[0] == 1 else 600)
        def send(command):
            if command["bri"] == 38:
                entered.set()
                release.wait(3)
            return {}
        transport.send.side_effect = send
        light = LightingControl(transport=transport, background=True)
        light.update(0)
        try:
            light.start_distance_sampling(read)
            self.assertTrue(entered.wait(1))
            self.assertTrue(sampled.wait(2))
            self.assertEqual(light.status()["target_brightness"], 128)
            release.set()
            with light._condition:
                self.assertTrue(light._condition.wait_for(
                    lambda: light._state["brightness"] == 128, timeout=2))
        finally:
            release.set()
            light.stop()

    def test_queued_auto_commands_coalesce_and_acknowledge_latest_sample(self):
        entered, release = threading.Event(), threading.Event()
        transport = MagicMock()
        commands = []
        def send(command):
            commands.append(command)
            if len(commands) == 1:
                entered.set()
                release.wait(2)
            return {}
        transport.send.side_effect = send
        light = LightingControl(transport=transport, background=True)
        light.update(0)
        try:
            light.update_distance(200, self.reading(100, timestamp="near"))
            self.assertTrue(entered.wait(1))
            light.update_distance(200, self.reading(350, timestamp="middle"))
            light.update_distance(200, self.reading(600, timestamp="far"))
            self.assertEqual(light.status()["target_brightness"], 128)
            self.assertIsNone(light.status()["brightness"])
            release.set()
            with light._condition:
                self.assertTrue(light._condition.wait_for(
                    lambda: light._state["brightness"] == 128, timeout=2))
            self.assertEqual([command["bri"] for command in commands], [38,128])
            self.assertEqual(light.status()["confirmed_radar_timestamp"], "far")
        finally:
            release.set()
            light.stop()
        self.assertFalse(commands[-1]["on"])

class LuxGatedAutoTests(unittest.TestCase):
    reading = DistanceLightingTests.reading

    def setUp(self):
        self.now = 0
        self.transport = MagicMock()
        self.transport.send.return_value = {}
        self.light = LightingControl(transport=self.transport, clock=lambda: self.now)

    def sample_distance(self, distance=350):
        self.light.update_distance(200, self.reading(distance))
        return self.light.status()

    def test_startup_middle_band_and_exact_hysteresis_boundaries(self):
        self.assertEqual(self.sample_distance()['brightness'], 0)
        self.assertEqual(self.light.status()['auto_gate_reason'], 'waiting_lux')
        for lux, ambient, target in ((10, 'unknown', 0), (30, 'unknown', 0), (15, 'unknown', 0),
                                     (9.99, 'dark', 83), (10, 'dark', 83),
                                     (20, 'dark', 83), (30, 'dark', 83),
                                     (30.01, 'light', 0), (30, 'light', 0),
                                     (20, 'light', 0), (10, 'light', 0), (9.99, 'dark', 83)):
            with self.subTest(lux=lux):
                self.light.update(lux)
                state = self.sample_distance()
                self.assertEqual(state['ambient_state'], ambient)
                self.assertEqual(state['brightness'], target)
                self.assertEqual(state['lux'], lux)
        self.assertEqual(state['lux_dark_threshold'], 10)
        self.assertEqual(state['lux_light_threshold'], 30)
        self.assertEqual(state['lux_stale_after_seconds'], 30)

    def test_daylight_stops_immediately_without_radar_and_color_cannot_bypass(self):
        self.light.update(0)
        self.sample_distance()
        self.light.update(31)
        self.assertFalse(self.transport.send.call_args.args[0]['on'])
        self.assertEqual(self.transport.send.call_args.args[0]['transition'], 0)
        state = self.light.configure(color='#123456')
        self.assertEqual(state['brightness'], 0)
        self.assertEqual(state['auto_gate_reason'], 'daylight')
        self.assertEqual(state['ambient_state'], 'light')
        self.assertEqual(self.light._desired[0], 0)

    def test_latest_lux_controls_independently_of_smoothed_value(self):
        self.light.update(600)
        self.now = 10
        self.light.update(0)
        self.assertEqual(self.sample_distance()['brightness'], 83)
        self.assertEqual(self.light.status()['smoothed_lux'], 300)

    def test_invalid_lux_and_recovery_require_new_dark_confirmation_and_radar(self):
        for invalid in (None, True, False, -1, float('nan'), float('inf'), '0'):
            with self.subTest(lux=invalid):
                self.light.update(0)
                self.assertEqual(self.sample_distance()['brightness'], 83)
                self.light.update(invalid)
                self.assertEqual(self.light.status()['brightness'], 0)
                self.assertEqual(self.light.status()['auto_gate_reason'], 'lux_unavailable')
                self.assertIsNone(self.light.status()['lux'])
                self.assertIsNone(self.light._auto_last_distance)
                self.light.update(15)
                self.assertEqual(self.sample_distance()['brightness'], 0)
                self.light.update(9)
                self.assertEqual(self.light.status()['brightness'], 0, 'Wait for new radar')
                self.assertEqual(self.sample_distance()['brightness'], 83)

    def test_staleness_is_passive_in_status_and_blocks_color_and_new_radar(self):
        self.light.update(0)
        self.sample_distance()
        self.now = 30
        self.assertEqual(self.light.status()['ambient_state'], 'dark')
        self.now = 30.01
        count = self.transport.send.call_count
        state = self.light.status()
        self.assertEqual(self.transport.send.call_count, count, 'Status must remain passive')
        self.assertEqual(state['ambient_state'], 'unknown')
        self.assertEqual(state['auto_gate_reason'], 'lux_unavailable')
        self.assertEqual(state['lux_age_seconds'], 30.01)
        self.assertEqual(self.light.configure(color='#abcdef')['brightness'], 0)
        self.assertEqual(self.sample_distance()['brightness'], 0)
        self.light.update(15)
        self.assertEqual(self.light.status()['ambient_state'], 'unknown')
        self.light.update(0)
        self.assertEqual(self.light.status()['brightness'], 0)
        self.assertEqual(self.sample_distance()['brightness'], 83)

    def test_lux_errors_radar_errors_and_controller_errors_are_separate(self):
        self.light.update(0)
        self.sample_distance()
        self.transport.send.side_effect = OSError('USB lost')
        self.light.update(None)
        self.light.update_distance(503, {})
        state = self.light.status()
        self.assertEqual(state['auto_gate_reason'], 'lux_unavailable')
        self.assertIn('Radar', state['sensor_error'])
        self.assertIn('USB lost', state['error'])
        self.light.update(0)
        self.assertIn('Radar', self.light.status()['sensor_error'])

    def test_manual_modes_and_distance_test_bypass_lux(self):
        for mode, target in (('on', 255), ('off', 0), ('distance_test', 64)):
            self.light.set_mode(mode)
            for lux in (None, 0, 600):
                self.light.update(lux)
                self.sample_distance()
                state = self.light.status()
                self.assertEqual(state['brightness'], target)
                self.assertIsNone(state['auto_gate_reason'])
        self.light.set_mode('auto')
        self.assertEqual(self.sample_distance()['brightness'], 0)

    def test_worker_watchdog_stops_stale_lux_with_fresh_radar(self):
        light = LightingControl(transport=self.transport, background=True, clock=lambda: self.now)
        self.addCleanup(light.stop)
        light.update(0)
        light.update_distance(200, self.reading(350))
        with light._condition:
            self.assertTrue(light._condition.wait_for(lambda: light._state['brightness'] == 83, timeout=1))
            self.now = 31
            # Keep radar fresh without invoking the lux gate through update_distance.
            light._radar_sampled_at = self.now
            light._condition.notify_all()
            self.assertTrue(light._condition.wait_for(lambda: light._state['brightness'] == 0, timeout=2))
        self.assertEqual(light.status()['auto_gate_reason'], 'lux_unavailable')
        self.assertNotIn('sensor_error', light.status())
        self.assertEqual(self.transport.send.call_args.args[0]['transition'], 0)

    def test_queued_on_replaced_and_inflight_ack_ignored_on_daylight_or_failure(self):
        for lux in (31, None):
            with self.subTest(lux=lux):
                entered, release = threading.Event(), threading.Event()
                commands = []
                transport = MagicMock()
                def send(command):
                    commands.append(command)
                    if len(commands) == 1:
                        entered.set()
                        release.wait(2)
                    return {}
                transport.send.side_effect = send
                light = LightingControl(transport=transport, background=True)
                try:
                    light.update(0)
                    light.update_distance(200, self.reading(100))
                    self.assertTrue(entered.wait(1))
                    light.update_distance(200, self.reading(600))
                    light.update(lux)
                    self.assertEqual(light.status()['target_brightness'], 0)
                    self.assertEqual(light._pending[0], 0)
                    release.set()
                    with light._condition:
                        self.assertTrue(light._condition.wait_for(lambda: light._state['brightness'] == 0, timeout=2))
                    self.assertEqual([c['bri'] for c in commands], [38, 0])
                finally:
                    release.set()
                    light.stop()

    def test_failed_on_retry_becomes_off_when_lux_expires(self):
        failed = threading.Event()
        commands = []
        def send(command):
            commands.append(command)
            if len(commands) == 1:
                failed.set()
                raise OSError('USB lost')
            return {}
        self.transport.send.side_effect = send
        light = LightingControl(transport=self.transport, background=True, clock=lambda: self.now)
        self.addCleanup(light.stop)
        light.update(0)
        light.update_distance(200, self.reading(350))
        self.assertTrue(failed.wait(1))
        with light._condition:
            self.assertTrue(light._condition.wait_for(lambda: light._state['status'] == 'controller_unavailable', timeout=1))
            self.now = 31
            light._radar_sampled_at = self.now
            light._condition.notify_all()
            self.assertTrue(light._condition.wait_for(lambda: light._state['brightness'] == 0, timeout=2))
        self.assertEqual([c['bri'] for c in commands], [83, 0])

    def test_direction_alignment_spacing_and_spc_calculation(self):
        transport = MagicMock()
        commands = []
        transport.send.side_effect = lambda cmd: commands.append(cmd) or {}
        light = LightingControl(transport=transport, background=False)
        self.addCleanup(light.stop)
        
        # Test max alignment 144 (spc=143)
        status = light.set_direction_alignment(144)
        self.assertEqual(status['mode'], 'direction_config')
        self.assertEqual(status['alignment'], 144)
        self.assertEqual(commands[-1]['seg'][0]['spc'], 143)
        self.assertEqual(commands[-1]['seg'][0]['grp'], 1)
        
        # Test min alignment 1 (spc=0)
        status = light.set_direction_alignment(1)
        self.assertEqual(status['alignment'], 1)
        self.assertEqual(commands[-1]['seg'][0]['spc'], 0)
        
        # Test alignment 72 (spc=71)
        status = light.set_direction_alignment(72)
        self.assertEqual(status['alignment'], 72)
        self.assertEqual(commands[-1]['seg'][0]['spc'], 71)
        
        # Bounds validation
        with self.assertRaises(ValueError):
            light.set_direction_alignment(0)
        with self.assertRaises(ValueError):
            light.set_direction_alignment(145)
        with self.assertRaises(ValueError):
            light.set_direction_alignment("not_a_number")

    def test_direction_beam_width_calculation(self):
        transport = MagicMock()
        commands = []
        transport.send.side_effect = lambda cmd: commands.append(cmd) or {}
        light = LightingControl(transport=transport, background=False)
        self.addCleanup(light.stop)

        # Spacing 10, default beam_width=1 -> grp=1, spc=9, of=0
        status = light.set_direction_alignment(10)
        self.assertEqual(status['beam_width'], 1)
        self.assertEqual(commands[-1]['seg'][0]['grp'], 1)
        self.assertEqual(commands[-1]['seg'][0]['spc'], 9)
        self.assertEqual(commands[-1]['seg'][0]['of'], 0)

        # Spacing 10, beam_width=3 -> grp=3, spc=7, of=0
        status = light.set_direction_alignment(10, beam_width=3)
        self.assertEqual(status['beam_width'], 3)
        self.assertEqual(commands[-1]['seg'][0]['grp'], 3)
        self.assertEqual(commands[-1]['seg'][0]['spc'], 7)
        self.assertEqual(commands[-1]['seg'][0]['of'], 0)

        # Spacing 10, beam_width=5 -> grp=5, spc=5, of=0
        status = light.set_direction_alignment(10, beam_width=5)
        self.assertEqual(status['beam_width'], 5)
        self.assertEqual(commands[-1]['seg'][0]['grp'], 5)
        self.assertEqual(commands[-1]['seg'][0]['spc'], 5)
        self.assertEqual(commands[-1]['seg'][0]['of'], 0)

        # Spacing 10, beam_width=11 -> gap fully lit, grp=1, spc=0, of=0
        status = light.set_direction_alignment(10, beam_width=11)
        self.assertEqual(status['beam_width'], 11)
        self.assertEqual(commands[-1]['seg'][0]['grp'], 1)
        self.assertEqual(commands[-1]['seg'][0]['spc'], 0)
        self.assertEqual(commands[-1]['seg'][0]['of'], 0)

        # Spacing 144, beam_width=3 -> grp=3, spc=141, of=0
        status = light.set_direction_alignment(144, beam_width=3)
        self.assertEqual(commands[-1]['seg'][0]['grp'], 3)
        self.assertEqual(commands[-1]['seg'][0]['spc'], 141)
        self.assertEqual(commands[-1]['seg'][0]['of'], 0)

        # Invalid beam_width validations
        with self.assertRaises(ValueError):
            light.set_direction_alignment(10, beam_width=0)
        with self.assertRaises(ValueError):
            light.set_direction_alignment(10, beam_width=2)  # Even width not allowed
        with self.assertRaises(ValueError):
            light.set_direction_alignment(10, beam_width=13)  # Exceeds max 11 for spacing 10
        with self.assertRaises(ValueError):
            light.set_direction_alignment(1, beam_width=3)  # Exceeds max 1 for spacing 1

    def test_direction_offset_calculation(self):
        transport = MagicMock()
        commands = []
        transport.send.side_effect = lambda cmd: commands.append(cmd) or {}
        light = LightingControl(transport=transport, background=False)
        self.addCleanup(light.stop)

        # Spacing 48, default offset=0
        status = light.set_direction_alignment(48)
        self.assertEqual(status['direction_offset'], 0)
        self.assertEqual(commands[-1]['seg'][0]['of'], 0)

        # Spacing 48, offset=12
        status = light.set_direction_alignment(48, offset=12)
        self.assertEqual(status['direction_offset'], 12)
        self.assertEqual(commands[-1]['seg'][0]['of'], 12)

        # Spacing 48, beam_width=3, offset=12
        status = light.set_direction_alignment(48, beam_width=3, offset=12)
        self.assertEqual(status['beam_width'], 3)
        self.assertEqual(status['direction_offset'], 12)
        self.assertEqual(commands[-1]['seg'][0]['grp'], 3)
        self.assertEqual(commands[-1]['seg'][0]['spc'], 45)
        self.assertEqual(commands[-1]['seg'][0]['of'], 12)

        # Offset bounds checks
        with self.assertRaises(ValueError):
            light.set_direction_alignment(48, offset=-1)
        with self.assertRaises(ValueError):
            light.set_direction_alignment(48, offset=48)
        with self.assertRaises(ValueError):
            light.set_direction_alignment(48, offset="twelve")

    def test_direction_config_mode_ignores_radar_and_lux(self):
        transport = MagicMock()
        commands = []
        transport.send.side_effect = lambda cmd: commands.append(cmd) or {}
        light = LightingControl(transport=transport, background=False)
        self.addCleanup(light.stop)
        
        light.set_direction_alignment(144)
        initial_commands_count = len(commands)
        
        # Lux updates should be ignored in direction_config mode
        light.update(0)
        light.update(100)
        self.assertEqual(len(commands), initial_commands_count)
        self.assertEqual(light.status()['mode'], 'direction_config')


# Share behaviors without inheriting single-LED-specific assertions.
for _name in ("test_lux_and_obsolete_radar_cannot_override_mode",
              "test_controller_error_separate_from_sensor_error",
              "test_sampling_interval_and_shutdown_off"):
    setattr(AutoLightingTests, _name, getattr(DistanceLightingTests, _name))


if __name__ == '__main__':
    unittest.main()
