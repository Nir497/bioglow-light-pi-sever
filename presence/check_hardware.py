"""Run with bioglow stopped. Capture actual binary UART reports; never transmit."""
import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from presence import PresenceSensor

parser = argparse.ArgumentParser()
parser.add_argument('--connected', action='store_true', help='Confirm the listed sensor wiring and power are connected')
args = parser.parse_args()
if not args.connected:
    parser.error('Check sensor wiring/power first, then pass --connected.')
port = Path('/dev/serial0')
try:
    mapped = port.resolve(strict=True)
except OSError as exc:
    parser.exit(1, f'/dev/serial0 unavailable: {exc}\n')
print(f'/dev/serial0 -> {mapped}', flush=True)
# Uses identical checks/settings to the service, with a Serial wrapper that
# preserves every received byte for later replay through the tested parser.
import serial
original = serial.Serial
class CapturingSerial(original):
    def read(self, size=1):
        data = super().read(size)
        capture.write(data); capture.flush()
        return data
serial.Serial = CapturingSerial
sensor = PresenceSensor()
last_timestamp = None
observed = set()
with Path('real-frames.bin').open('xb') as capture:
    sensor.start()
    try:
        for instruction in ('Leave the area; keep other people outside detection range for 20 seconds.',
                            'Walk in front of the sensor for 20 seconds.',
                            'Stand still in front of it for 20 seconds.',
                            'Leave its detection area again for 20 seconds.'):
            input(instruction + ' Press Enter when ready. ')
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status, reading = sensor.response()
                if status != 200:
                    print(json.dumps(reading), flush=True)
                    if not reading['serial_connected']:
                        raise RuntimeError(reading['error']['message'])
                elif reading['timestamp'] != last_timestamp:
                    observed.add(reading['presence'])
                    last_timestamp = reading['timestamp']
                    print(json.dumps(reading), flush=True)
                time.sleep(0.5)
    finally:
        sensor.stop()
print('Observed actual states:', sorted(observed))
if not {'none', 'moving', 'stationary'} <= observed:
    sys.exit('Hardware test incomplete: inspect the capture and repeat missing states; both is also a valid sensor state.')
