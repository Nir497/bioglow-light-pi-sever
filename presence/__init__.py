"""Passive LD2410C receiver; manufacturer protocol v1.00, section 2.3."""
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HEADER = bytes.fromhex('f4 f3 f2 f1')
TAIL = bytes.fromhex('f8 f7 f6 f5')

class Parser:
    def __init__(self):
        self.buffer = bytearray()
        self.malformed = 0

    def feed(self, chunk):
        self.buffer.extend(chunk)
        readings = []
        while True:
            start = self.buffer.find(HEADER)
            if start < 0:
                self.buffer[:] = self.buffer[-3:]
                break
            del self.buffer[:start]
            if len(self.buffer) < 6:
                break
            size = int.from_bytes(self.buffer[4:6], 'little')
            # Normal reporting has exactly 13 payload bytes. Engineering reports
            # are deliberately rejected: we never enable or change that mode.
            if size != 13:
                self.malformed += 1
                del self.buffer[0]
                continue
            if len(self.buffer) < 23:
                break
            p = self.buffer[6:19]
            if (self.buffer[19:23] != TAIL or p[:2] != b'\x02\xaa'
                    or p[-2:] != b'\x55\x00' or p[2] > 3
                    or p[5] > 100 or p[8] > 100):
                self.malformed += 1
                del self.buffer[0]
                continue
            state = p[2]
            readings.append({
                'presence': ('none', 'moving', 'stationary', 'both')[state],
                'moving_distance_cm': int.from_bytes(p[3:5], 'little') if state & 1 else None,
                'stationary_distance_cm': int.from_bytes(p[6:8], 'little') if state & 2 else None,
                'moving_energy_score': p[5], 'stationary_energy_score': p[8],
                'energy_units': 'sensor score (0–100), not percentage or probability',
            })
            del self.buffer[:23]
        return readings

class PresenceSensor:
    def __init__(self, stale_after=2.0):
        self.stale_after = stale_after
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.latest = None
        self.received_at = None
        self.connected = False
        self.mapping = None
        self.error = 'Serial worker has not started.'
        self.valid_frames = 0
        self.malformed_frames = 0

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='ld2410c', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                port = Path('/dev/serial0')
                mapping = str(port.resolve(strict=True))
                cmdline = Path('/proc/cmdline').read_text()
                if re.search(r'\bconsole=(?:serial0|' + re.escape(Path(mapping).name) + r')(?:,|\s|$)', cmdline):
                    raise RuntimeError('Serial login/kernel console is enabled. Disable it and reboot before reading radar.')
                import serial
                # Linux pyserial supports 256000 via termios2/BOTHER. Exclusive
                # ownership and no flow control; no writes or configuration commands.
                with serial.Serial(str(port), 256000, bytesize=serial.EIGHTBITS,
                                   parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                   timeout=0.2, exclusive=True, xonxoff=False,
                                   rtscts=False, dsrdtr=False) as connection:
                    parser = Parser()
                    with self.lock:
                        self.connected, self.mapping, self.error = True, mapping, None
                        self.latest, self.received_at = None, None
                    while not self.stop_event.is_set():
                        data = connection.read(256)
                        for reading in parser.feed(data):
                            reading['timestamp'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                            with self.lock:
                                self.latest = reading
                                self.received_at = time.monotonic()
                                self.valid_frames += 1
                        with self.lock:
                            self.malformed_frames = parser.malformed
            except Exception as exc:
                with self.lock:
                    self.connected = False
                    self.error = f'LD2410C /dev/serial0: {exc}'
                self.stop_event.wait(2)
        with self.lock:
            self.connected = False
            self.error = 'Serial worker stopped.'

    def response(self, health=False):
        with self.lock:
            age = None if self.received_at is None else time.monotonic() - self.received_at
            fresh = self.connected and age is not None and age <= self.stale_after
            payload = {'sensor': 'LD2410C', 'serial_connected': self.connected,
                       'serial_port': '/dev/serial0', 'serial_mapping': self.mapping,
                       'fresh': fresh, 'age_seconds': age,
                       'stale_after_seconds': self.stale_after,
                       'timestamp': self.latest['timestamp'] if self.latest else None}
            if health:
                payload.update(valid_frames=self.valid_frames, malformed_frames=self.malformed_frames)
            elif fresh:
                payload.update(self.latest)
            if not fresh:
                payload['status'] = 'unavailable'
                payload['error'] = {'code': 'presence_unavailable', 'message': self.error or
                    ('No valid normal sensor frame received.' if age is None else 'Latest sensor frame is stale.')}
                # Never expose old presence/distance values as current readings.
                return 503, payload
            payload['status'] = 'ok'
            return 200, payload

def health_response(light_sensor, presence_sensor):
    # Keep legacy VEML health fields and its real read/failure semantics.
    light_status, payload = light_sensor.response(health=True)
    radar_status, radar = presence_sensor.response(health=True)
    payload['presence_sensor'] = radar
    payload['serial_connected'] = radar['serial_connected']
    payload['fresh'] = radar['fresh']
    if radar_status != 200:
        payload['status'] = 'error'
    return max(light_status, radar_status), payload
