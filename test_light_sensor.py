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
            def light(self):
                self.active += 1
                self.peak = max(self.peak, self.active)
                time.sleep(0.005)
                self.active -= 1
                return 125
            white = 203
            def gain_value(self): return 0.125
            def integration_time_value(self): return 100
            def resolution(self): return 0.1
        sensor = LightSensor()
        device = sensor._sensor = Device()
        with patch('light_sensor.Path.exists', return_value=True):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: sensor.read(), range(24)))
        self.assertEqual(device.peak, 1)
        self.assertTrue(all(r['lux'] == 12.5 and r['timestamp'].endswith('Z') for r in results))

    def test_all_channels_and_settings_are_returned_for_reading_and_health(self):
        class Device:
            light = 100
            white = 234
            def gain_value(self): return 0.125
            def integration_time_value(self): return 100
            def resolution(self): return 0.5376
        for health in (False, True):
            sensor = LightSensor()
            sensor._sensor = Device()
            with patch('light_sensor.Path.exists', return_value=True):
                status, reading = sensor.response(health)
            self.assertEqual(status, 200)
            self.assertAlmostEqual(reading['lux'], 53.76)
            self.assertEqual(reading['ambient_counts'], 100)
            self.assertEqual(reading['white_counts'], 234)
            self.assertEqual(reading['gain'], 0.125)
            self.assertEqual(reading['integration_time_ms'], 100)
            self.assertEqual(reading['resolution_lux_per_count'], 0.5376)
            json.dumps(reading, allow_nan=False)

    def test_zero_and_maximum_raw_counts_are_valid(self):
        class Device:
            def gain_value(self): return 0.125
            def integration_time_value(self): return 100
            def resolution(self): return 0.5376
        for value in (0, 65535):
            sensor = LightSensor()
            sensor._sensor = Device()
            sensor._sensor.light = sensor._sensor.white = value
            with patch('light_sensor.Path.exists', return_value=True):
                status, reading = sensor.response()
            self.assertEqual(status, 200)
            self.assertEqual(reading['ambient_counts'], value)
            self.assertEqual(reading['white_counts'], value)

    def test_failed_white_channel_does_not_publish_partial_reading(self):
        class Device:
            light = 100
            @property
            def white(self): raise OSError('White channel read failed')
        sensor = LightSensor()
        sensor._sensor = Device()
        with patch('light_sensor.Path.exists', return_value=True):
            status, reading = sensor.response()
        self.assertEqual(status, 503)
        self.assertNotIn('lux', reading)
        self.assertNotIn('ambient_counts', reading)
        self.assertNotIn('white_counts', reading)

    def test_failed_read_resets_device_and_never_returns_zero(self):
        class Device:
            @property
            def light(self):
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
