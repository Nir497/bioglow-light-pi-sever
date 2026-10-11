"""Serialized, real VEML7700 access on Raspberry Pi I2C bus 1."""
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class SensorError(Exception):
    pass


class LightSensor:
    def __init__(self):
        self._lock = threading.Lock()
        self._bus = None
        self._sensor = None

    def read(self):
        with self._lock:
            try:
                if not Path('/dev/i2c-1').exists():
                    raise SensorError('I2C bus 1 is unavailable: /dev/i2c-1 is missing. Enable I2C on the Pi and ensure this process can access the device.')
                if self._sensor is None:
                    from adafruit_extended_bus import ExtendedI2C
                    import adafruit_veml7700
                    self._bus = ExtendedI2C(1)
                    # The driver probes the exact address; no other buses are used.
                    self._sensor = adafruit_veml7700.VEML7700(self._bus, address=0x10)
                    self._sensor.light_gain = self._sensor.ALS_GAIN_1_8
                    self._sensor.light_integration_time = self._sensor.ALS_100MS
                    time.sleep(0.2)  # Allow the first conversion to complete.
                lux = float(self._sensor.lux)
                if not math.isfinite(lux) or lux < 0:
                    raise ValueError('Sensor returned an invalid lux value')
                return {'lux': lux, 'timestamp': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'sensor': 'VEML7700'}
            except Exception as exc:
                self._sensor = None
                if self._bus is not None:
                    try:
                        self._bus.deinit()
                    except Exception:
                        pass
                    self._bus = None
                raise SensorError(f'VEML7700 at bus 1 address 0x10: {exc}') from exc

    def response(self, health=False):
        try:
            reading = self.read()
            if health:
                return 200, {'status': 'ok', 'service': 'BioGlow BirdCast', 'sensor': reading['sensor'], 'sensor_connected': True, 'timestamp': reading['timestamp'], 'lux': reading['lux']}
            return 200, reading
        except SensorError as exc:
            return 503, {'status': 'error', 'sensor': 'VEML7700', 'sensor_connected': False, 'error': {'code': 'sensor_unavailable', 'message': str(exc)}}
