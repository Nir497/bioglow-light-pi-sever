import unittest
import json
from unittest.mock import MagicMock

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
