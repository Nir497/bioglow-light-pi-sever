import importlib.util
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from presence import Parser, PresenceEstimator, PresenceSensor, health_response, HEADER, TAIL

# Manufacturer's published normal report example. Not a captured hardware frame.
EXAMPLE = bytes.fromhex('f4 f3 f2 f1 0d 00 02 aa 02 51 00 00 00 00 3b 00 00 55 00 f8 f7 f6 f5')
def frame(state=3, mov_d=300, mov_e=60, stat_d=150, stat_e=57, det_d=150):
    payload = (
        bytes.fromhex('02 aa')
        + bytes([state])
        + int(mov_d).to_bytes(2, 'little')
        + bytes([mov_e])
        + int(stat_d).to_bytes(2, 'little')
        + bytes([stat_e])
        + int(det_d).to_bytes(2, 'little')
        + bytes.fromhex('55 00')
    )
    return HEADER + len(payload).to_bytes(2, 'little') + payload + TAIL

def engineering_frame(state=3, mov_d=120, mov_e=75, stat_d=115, stat_e=82, det_d=118,
                      mov_gates=(10, 75, 30, 5, 0, 0, 0, 0, 0),
                      stat_gates=(15, 82, 40, 8, 2, 0, 0, 0, 0)):
    body = (
        bytes.fromhex('01 aa')
        + bytes([state])
        + int(mov_d).to_bytes(2, 'little')
        + bytes([mov_e])
        + int(stat_d).to_bytes(2, 'little')
        + bytes([stat_e])
        + int(det_d).to_bytes(2, 'little')
        + bytes([len(mov_gates) - 1, len(stat_gates) - 1])
        + bytes(mov_gates)
        + bytes(stat_gates)
        + bytes.fromhex('55 00')
    )
    return HEADER + len(body).to_bytes(2, 'little') + body + TAIL


class Tests(unittest.TestCase):
    def test_every_split(self):
        for n in range(1, len(EXAMPLE)):
            p = Parser()
            self.assertEqual(p.feed(EXAMPLE[:n]), [])
            r = p.feed(EXAMPLE[n:])[0]
            self.assertEqual(r['presence'], 'stationary')
            self.assertIsNone(r['moving_distance_cm'])
            self.assertEqual(r['stationary_energy_score'], 59)
            self.assertEqual(r['detection_distance_cm'], 0)

    def test_states_and_endian(self):
        for state in range(4):
            r = Parser().feed(frame(state, det_d=145))[0]
            self.assertEqual(r['moving_distance_cm'], 300 if state & 1 else None)
            self.assertEqual(r['stationary_distance_cm'], 150 if state & 2 else None)
            self.assertEqual(r['detection_distance_cm'], 145 if state else None)

    def test_engineering_mode_frame_parsing(self):
        r = Parser().feed(engineering_frame())[0]
        self.assertEqual(r['reporting_mode'], 'engineering')
        self.assertEqual(r['presence'], 'both')
        self.assertEqual(r['detection_distance_cm'], 118)
        self.assertEqual(len(r['moving_gate_energy']), 9)
        self.assertEqual(r['moving_gate_energy'][1], 75)
        self.assertEqual(r['stationary_gate_energy'][1], 82)

    def test_noise_bad_frames_and_concatenation(self):
        corrupt = bytearray(frame()); corrupt[-1] = 0
        bad_state = frame(4)
        p = Parser()
        r = p.feed(b'noise' + HEADER + b'\xff\xff' + corrupt + bad_state + frame() + EXAMPLE)
        self.assertEqual(len(r), 2)
        self.assertGreaterEqual(p.malformed, 3)

    def test_truncated_frame_recovery(self):
        p = Parser()
        self.assertEqual(len(p.feed(frame()[:10] + EXAMPLE)), 1)
        p.feed(b'x' * 100000)
        self.assertLessEqual(len(p.buffer), 3)

    def test_bad_markers_energy_and_type(self):
        for offset, value in [(6,1),(7,0),(11,101),(14,101),(17,0),(18,1)]:
            bad = bytearray(frame()); bad[offset] = value
            self.assertEqual(len(Parser().feed(bad+EXAMPLE)), 1)

    def test_estimator_debounce_hysteresis_and_range_trend(self):
        est = PresenceEstimator()
        t = 10.0
        for _ in range(5):
            snap = est.update(Parser().feed(frame(0, mov_e=0, stat_e=8, det_d=0))[0], now=t)
            t += 0.1
        self.assertEqual(snap['presence_state'], 'EMPTY')
        self.assertFalse(snap['presence_confirmed'])

        # Single transient stationary spike should become APPROACHING_CANDIDATE, not confirmed
        snap = est.update(Parser().feed(frame(2, mov_e=0, stat_d=160, stat_e=65, det_d=160))[0], now=t)
        self.assertEqual(snap['presence_state'], 'APPROACHING_CANDIDATE')
        self.assertFalse(snap['presence_confirmed'])

        # Repeated approaching detections confirm PRESENT_ACTIVE with CLOSER range trend
        for dist in (200, 185, 160, 130, 105):
            t += 0.2
            snap = est.update(Parser().feed(frame(3, mov_d=dist, mov_e=80, stat_d=dist, stat_e=90, det_d=dist))[0], now=t)
        self.assertEqual(snap['presence_state'], 'PRESENT_ACTIVE')
        self.assertTrue(snap['presence_confirmed'])
        self.assertEqual(snap['range_trend'], 'CLOSER')
        self.assertGreater(snap['activity_level'], 0.5)

        # Brief drop-out holds presence via asymmetric debounce before returning to EMPTY
        for _ in range(5):
            t += 0.4
            snap = est.update(Parser().feed(frame(0, mov_e=0, stat_e=8, det_d=0))[0], now=t)
        self.assertIn(snap['presence_state'], ('PRESENT_STILL', 'DEPARTING_CANDIDATE'))
        self.assertTrue(snap['presence_confirmed'])

        t += 5.0
        for _ in range(5):
            t += 0.1
            snap = est.update(Parser().feed(frame(0, mov_e=0, stat_e=8, det_d=0))[0], now=t)
        self.assertEqual(snap['presence_state'], 'EMPTY')
        self.assertFalse(snap['presence_confirmed'])

    def test_fresh_stale_disconnected(self):
        s = PresenceSensor()
        self.assertEqual(s.response()[0], 503)
        s.connected = True
        s.latest = {**Parser().feed(frame())[0], 'timestamp': '2026-10-11T00:00:00Z'}
        s.received_at = time.monotonic()
        status, ok_payload = s.response()
        self.assertEqual(status, 200)
        self.assertIn('presence_state', ok_payload)
        self.assertTrue(ok_payload['sensor_healthy'])
        s.received_at -= 3
        status, r = s.response()
        self.assertEqual(status, 503)
        self.assertNotIn('presence', r)
        self.assertFalse(r['fresh'])
        self.assertEqual(r['presence_state'], 'UNKNOWN')
        s.received_at = time.monotonic(); s.connected = False
        self.assertEqual(s.response()[0], 503)

    def test_both_handlers_with_unavailable_radar(self):
        import birdcast_service
        import tempfile
        targets = []
        class Light:
            def response(self, health=False):
                return (503, {'sensor_connected': False, 'status': 'error'}) if health else (200, {'lux': 42})
        with tempfile.TemporaryDirectory() as directory:
            local = birdcast_service.Service(Path(directory), Path(directory) / 'config.json')
            local.light_sensor = Light()
            targets.append((birdcast_service, local))
            legacy_path = Path('/home/nir/bioglow-birdcast.py')
            if legacy_path.exists():
                spec = importlib.util.spec_from_file_location('combined_radar', str(legacy_path))
                combined = importlib.util.module_from_spec(spec); spec.loader.exec_module(combined)
                targets.append((combined, combined.Service()))
            for module, service in targets:
                handler = module.make_handler(service)
                expected_reading = 200 if module is birdcast_service else 404
                for endpoint, expected in [('/presence',503),('/health',503),('/reading',expected_reading)]:
                    h = object.__new__(handler)
                    h.path = endpoint; h.command = 'GET'; h.wfile = io.BytesIO()
                    h.connection = unittest.mock.Mock()
                    responses = []
                    h.send_response = lambda status: responses.append(status)
                    h.send_header = lambda *args: None
                    h.end_headers = lambda: None
                    h.do_GET()
                    self.assertEqual(responses, [expected])
                    payload = json.loads(h.wfile.getvalue())
                    if endpoint == '/health': self.assertIn('presence_sensor', payload)
                    if endpoint == '/reading':
                        if module is not birdcast_service: self.assertEqual(payload['error']['code'], 'not_found')
                        else: self.assertEqual(payload['lux'], 42)

    def test_worker_never_writes_and_stops(self):
        import types
        s = PresenceSensor()
        class Port:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, n):
                s.stop_event.set()
                return EXAMPLE
        fake = types.SimpleNamespace(Serial=unittest.mock.Mock(return_value=Port()),
                                     EIGHTBITS=8, PARITY_NONE='N', STOPBITS_ONE=1)
        with patch.dict(sys.modules, {'serial':fake}), patch.object(Path, 'resolve', return_value=Path('/dev/ttyAMA0')), patch.object(Path, 'read_text', return_value='console=tty1'):
            s._run()
        self.assertEqual(s.valid_frames, 1)
        self.assertEqual(fake.Serial.call_args.kwargs['exclusive'], True)
        self.assertFalse(s.connected)

if __name__ == '__main__': unittest.main()
