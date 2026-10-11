"""Unit tests use explicit test doubles; production never simulates readings."""
import importlib.util
import json
import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from light_sensor import LightSensor
import birdcast_service


class SensorTests(unittest.TestCase):
    def test_missing_bus_is_descriptive_503_without_lux(self):
        with patch('light_sensor.Path.exists', return_value=False):
            for health in (False, True):
                status, payload = LightSensor().response(health)
                self.assertEqual(status, 503)
                self.assertNotIn('lux', payload)
                self.assertFalse(payload['sensor_connected'])
                self.assertIn('/dev/i2c-1', payload['error']['message'])
                json.dumps(payload, allow_nan=False)

    def test_access_serialized_and_timestamp_utc(self):
        class Device:
            active = 0
            peak = 0
            @property
            def lux(self):
                self.active += 1
                self.peak = max(self.peak, self.active)
                time.sleep(0.005)
                self.active -= 1
                return 12.5
        sensor = LightSensor()
        device = sensor._sensor = Device()
        with patch('light_sensor.Path.exists', return_value=True):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: sensor.read(), range(24)))
        self.assertEqual(device.peak, 1)
        self.assertTrue(all(r['lux'] == 12.5 and r['timestamp'].endswith('Z') for r in results))

    def test_failed_read_resets_device_and_never_returns_zero(self):
        class Device:
            @property
            def lux(self):
                raise OSError('Remote I/O error')
        sensor = LightSensor()
        sensor._sensor = Device()
        with patch('light_sensor.Path.exists', return_value=True):
            status, payload = sensor.response()
        self.assertEqual(status, 503)
        self.assertNotIn('lux', payload)
        self.assertIn('Remote I/O error', payload['error']['message'])
        self.assertIsNone(sensor._sensor)

    def test_both_existing_http_handlers_report_real_missing_bus(self):
        spec = importlib.util.spec_from_file_location('combined', Path(__file__).parent.parent / 'bioglow-birdcast.py')
        combined = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(combined)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        services = [
            (birdcast_service, birdcast_service.Service(Path(directory.name), Path(directory.name) / "config.json"), '_send'),
            (combined, combined.Service(), 'json_response'),
        ]
        with patch('light_sensor.Path.exists', return_value=False):
            for module, service, sender in services:
                for endpoint in ('/reading', '/health'):
                    handler = object.__new__(module.make_handler(service))
                    handler.path = endpoint
                    handler.command = 'GET'
                    handler.connection = unittest.mock.Mock()
                    output = []
                    setattr(handler, sender, lambda status, payload: output.append((status, payload)))
                    handler.do_GET()
                    expected_status = 404 if module is combined and endpoint == '/reading' else 503
                    self.assertEqual(output[0][0], expected_status)
                    if module is birdcast_service:
                        self.assertFalse(output[0][1]['sensor_connected'])
                        self.assertNotIn('lux', output[0][1])
                    if endpoint == '/health':
                        self.assertFalse(output[0][1]['serial_connected'])


if __name__ == '__main__':
    unittest.main()
