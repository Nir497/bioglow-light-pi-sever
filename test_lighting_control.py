import unittest
import json
import threading
import socket
from unittest.mock import MagicMock, patch

from lighting_control import LightingControl


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
                light.update(0)
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

    def test_hysteresis_and_brightness(self):
        self.assertEqual(self.read(0)['brightness'], 255)
        self.assertEqual(self.read(100)['brightness'], 50)
        self.assertEqual(self.read(301)['brightness'], 0)
        self.assertEqual(self.read(100)['brightness'], 0)
        self.assertEqual(self.read(30)['brightness'], 0)
        self.assertEqual(self.read(29)['brightness'], 57)
        self.assertEqual(self.read(300)['brightness'], 50)

    def test_unavailable_retains_output(self):
        self.read(0)
        for value in (None, float('nan'), -1):
            self.assertEqual(self.read(value)['brightness'], 255)
            self.assertEqual(self.light.status()['status'], 'sensor_unavailable')

    def test_manual_override_survives_reads_and_auto_restores_lux_control(self):
        self.read(500)
        self.assertEqual(self.light.set_mode('on')['brightness'], 255)
        self.assertEqual(self.read(600)['brightness'], 255)
        self.assertEqual(self.light.set_mode('auto')['brightness'], 0)
        self.read(0)
        self.assertEqual(self.light.set_mode('off')['brightness'], 0)
        self.assertEqual(self.read(0)['brightness'], 0)
        self.assertEqual(self.light.set_mode('auto')['brightness'], 255)

    def test_manual_works_without_sensor_and_rejects_invalid_modes(self):
        self.assertEqual(self.light.set_mode('on')['brightness'], 255)
        self.assertEqual(self.read(None)['brightness'], 255)
        self.assertEqual(self.light.set_mode('auto')['status'], 'waiting')
        with self.assertRaises(ValueError):
            self.light.set_mode('invalid')

    def test_failure_retries_without_claiming_success(self):
        self.opener.side_effect = OSError('offline')
        self.assertIsNone(self.read(0)['brightness'])
        self.assertEqual(self.light.status()['target_brightness'], 255)
        self.read(0)
        self.assertEqual(self.opener.call_count, 2)

    def test_smoothing_and_wled_command(self):
        self.light.update(600)
        self.now = 10
        self.light.update(0)
        self.assertEqual(self.light.status()['smoothed_lux'], 300)
        self.assertEqual(self.light.status()['brightness'], 0)
        request = self.opener.call_args.args[0]
        payload = json.loads(request.data)
        self.assertFalse(payload['on'])
        self.assertEqual(payload['transition'], 30)
        self.assertEqual(payload['lor'], 2)
        self.assertIn('bioglow.local/json/state', request.full_url)


if __name__ == '__main__':
    unittest.main()
