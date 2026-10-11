"""Interactive hardware verification. No simulated readings."""
import json
import statistics
import time
from light_sensor import LightSensor

sensor = LightSensor()

def sample():
    readings = []
    for _ in range(5):
        reading = sensor.read()  # Abort immediately if hardware access fails.
        print(json.dumps(reading), flush=True)
        readings.append(reading['lux'])
        time.sleep(0.3)
    return statistics.median(readings)

input('Leave the sensor uncovered, then press Enter: ')
uncovered = sample()
input('Cover the sensor fully, then press Enter: ')
covered = sample()
input('Uncover the sensor, then press Enter: ')
restored = sample()
print(json.dumps({'uncovered_lux': uncovered, 'covered_lux': covered,
                  'restored_lux': restored, 'cover_reduced_reading': covered < uncovered,
                  'uncover_increased_reading': restored > covered}))
