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
        for distance, target in ((1,38), (100,38), (350,147), (600,255), (900,255)):
            self.assertEqual(distance_brightness(distance), target)
            self.light.update_distance(200, dict(fresh=True, presence='moving', range_estimate_cm=distance))
            self.assertEqual(self.light.status()['brightness'], target)

    def test_unavailable_radar_turns_off_immediately(self):
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
        self.opener.side_effect = OSError('offline')
        self.light.update_distance(200, dict(fresh=True, presence='moving', range_estimate_cm=600))
        self.assertIsNone(self.light.status()['brightness'])
        self.assertEqual(self.light.status()['target_brightness'], 255)
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
        for distance, index in ((1, 0), (99, 0), (100, 0), (101, 1), (350, 96), (599, 190), (600, 191), (700, 191)):
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
            self.assertEqual(status['led_index'], 191)
            self.assertIsNone(status['confirmed_led_index'])
            release.set()
            self.assertTrue(final_ack.wait(2))
        finally:
            release.set()
            light.stop()
        self.assertEqual(calls[2]['seg'][0]['start'], 191)

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

    reading = DistanceLightingTests.reading
    def test_auto_color_and_full_strip(self):
        self.light.update_distance(200, self.reading(350))
        self.light.configure(color="#123456")
        state = self.light.status()
        self.assertEqual(state["mode"], "auto")
        self.assertEqual(state["brightness"], 147)
        command = self.transport.send.call_args.args[0]
        self.assertEqual(command["transition"], 3)
        self.assertNotIn("start", command["seg"][0])
        self.assertEqual(command["seg"][0]["col"], [[18,52,86,0]])
        self.light.set_mode("on")
        self.assertEqual(self.light.status()["brightness"], 255)

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
        try:
            light.update_distance(200, self.reading(100, timestamp="near"))
            self.assertTrue(entered.wait(1))
            light.update_distance(200, self.reading(350, timestamp="middle"))
            light.update_distance(200, self.reading(600, timestamp="far"))
            self.assertEqual(light.status()["target_brightness"], 255)
            self.assertIsNone(light.status()["brightness"])
            release.set()
            with light._condition:
                self.assertTrue(light._condition.wait_for(
                    lambda: light._state["brightness"] == 255, timeout=2))
            self.assertEqual([command["bri"] for command in commands], [38,255])
            self.assertEqual(light.status()["confirmed_radar_timestamp"], "far")
        finally:
            release.set()
            light.stop()
        self.assertFalse(commands[-1]["on"])

# Share behaviors without inheriting single-LED-specific assertions.
for _name in ("test_lux_and_obsolete_radar_cannot_override_mode",
              "test_controller_error_separate_from_sensor_error",
              "test_unchanged_output_is_deduplicated_and_periodically_reasserted",
              "test_sampling_interval_and_shutdown_off"):
    setattr(AutoLightingTests, _name, getattr(DistanceLightingTests, _name))


if __name__ == '__main__':
    unittest.main()
