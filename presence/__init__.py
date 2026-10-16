"""Passive LD2410C receiver and sliding-window presence/activity estimator.

Implements manufacturer protocol v1.00 (section 2.3 normal and engineering reports)
and the BioGlow feature/state estimator contract from the LD2410C build plan.
"""
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import re
import statistics
import threading
import time

HEADER = bytes.fromhex('f4 f3 f2 f1')
TAIL = bytes.fromhex('f8 f7 f6 f5')
STATES = ('none', 'moving', 'stationary', 'both')
GATE_WIDTH_CM = 75
GATE_COUNT = 9
ENERGY_UNITS = 'sensor score (0–100), not percentage or probability'


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
            if start > 0:
                del self.buffer[:start]
            if len(self.buffer) < 6:
                break
            size = int.from_bytes(self.buffer[4:6], 'little')
            if size < 13 or size > 64:
                self.malformed += 1
                del self.buffer[0]
                continue
            frame_len = 6 + size + 4
            if len(self.buffer) < frame_len:
                break
            p = self.buffer[6:6 + size]
            tail = self.buffer[6 + size:frame_len]
            if tail != TAIL or p[1:2] != b'\xaa' or p[-2:] != b'\x55\x00':
                self.malformed += 1
                del self.buffer[0]
                continue
            report_type = p[0]
            state = p[2]
            mov_e = p[5]
            stat_e = p[8]
            if state > 3 or mov_e > 100 or stat_e > 100:
                self.malformed += 1
                del self.buffer[0]
                continue

            mov_d = int.from_bytes(p[3:5], 'little')
            stat_d = int.from_bytes(p[6:8], 'little')
            det_d = int.from_bytes(p[9:11], 'little')

            if report_type == 0x02:
                if size != 13:
                    self.malformed += 1
                    del self.buffer[0]
                    continue
                reading = {
                    'presence': STATES[state],
                    'target_state': state,
                    'reporting_mode': 'normal',
                    'moving_distance_cm': mov_d if state & 1 else None,
                    'stationary_distance_cm': stat_d if state & 2 else None,
                    'detection_distance_cm': det_d if state else None,
                    'raw_moving_distance_cm': mov_d,
                    'raw_stationary_distance_cm': stat_d,
                    'raw_detection_distance_cm': det_d,
                    'moving_energy_score': mov_e,
                    'stationary_energy_score': stat_e,
                    'energy_units': ENERGY_UNITS,
                }
            elif report_type == 0x01:
                if size < 15:
                    self.malformed += 1
                    del self.buffer[0]
                    continue
                max_mov_gate = p[11]
                max_stat_gate = p[12]
                m_count = max_mov_gate + 1
                s_count = max_stat_gate + 1
                if (m_count > 16 or s_count > 16
                        or 13 + m_count + s_count > size - 2):
                    self.malformed += 1
                    del self.buffer[0]
                    continue
                mov_gates = list(p[13:13 + m_count])
                stat_gates = list(p[13 + m_count:13 + m_count + s_count])
                if any(e > 100 for e in mov_gates) or any(e > 100 for e in stat_gates):
                    self.malformed += 1
                    del self.buffer[0]
                    continue
                reading = {
                    'presence': STATES[state],
                    'target_state': state,
                    'reporting_mode': 'engineering',
                    'moving_distance_cm': mov_d if state & 1 else None,
                    'stationary_distance_cm': stat_d if state & 2 else None,
                    'detection_distance_cm': det_d if state else None,
                    'raw_moving_distance_cm': mov_d,
                    'raw_stationary_distance_cm': stat_d,
                    'raw_detection_distance_cm': det_d,
                    'moving_energy_score': mov_e,
                    'stationary_energy_score': stat_e,
                    'max_moving_gate': max_mov_gate,
                    'max_stationary_gate': max_stat_gate,
                    'moving_gate_energy': mov_gates,
                    'stationary_gate_energy': stat_gates,
                    'energy_units': ENERGY_UNITS,
                }
            else:
                self.malformed += 1
                del self.buffer[0]
                continue

            readings.append(reading)
            del self.buffer[:frame_len]
        return readings


def _frame_distance_cm(reading):
    state = reading.get('target_state')
    if state is None:
        state = STATES.index(reading['presence']) if reading.get('presence') in STATES else 0
    mov_d = reading.get('moving_distance_cm')
    stat_d = reading.get('stationary_distance_cm')
    det_d = reading.get('detection_distance_cm')
    if (state & 1) and isinstance(mov_d, (int, float)) and mov_d > 0:
        if (state & 2) and isinstance(stat_d, (int, float)) and stat_d > 0 and abs(mov_d - stat_d) <= 60:
            return round((mov_d + stat_d) / 2)
        return int(mov_d)
    if (state & 2) and isinstance(stat_d, (int, float)) and stat_d > 0:
        return int(stat_d)
    if state > 0 and isinstance(det_d, (int, float)) and det_d > 0:
        return int(det_d)
    return None


class PresenceEstimator:
    """Sliding-window feature extractor and debounced state estimator for LD2410C."""

    def __init__(self, window_seconds=8.0, recent_seconds=2.5, hold_seconds=4.5, min_confirm_frames=3):
        self.window_seconds = window_seconds
        self.recent_seconds = recent_seconds
        self.hold_seconds = hold_seconds
        self.min_confirm_frames = min_confirm_frames
        self.samples = deque()
        self.baseline_stationary_energy = 10.0
        self.state = 'EMPTY'
        self.confirmed = False
        self.last_confirmed_at = None
        self.last_range_cm = None

    def reset(self):
        self.samples.clear()
        self.state = 'EMPTY'
        self.confirmed = False
        self.last_confirmed_at = None
        self.last_range_cm = None

    def _is_credible(self, reading):
        state = reading.get('target_state')
        if state is None:
            state = STATES.index(reading['presence']) if reading.get('presence') in STATES else 0
        mov_e = reading.get('moving_energy_score') or 0
        stat_e = reading.get('stationary_energy_score') or 0
        stat_threshold = max(22.0, min(65.0, self.baseline_stationary_energy + 12.0))
        has_motion = bool(state & 1) and (mov_e >= 15 or reading.get('moving_distance_cm') is not None)
        has_still = bool(state & 2) and stat_e >= stat_threshold
        if state == 2 and len(self.samples) <= 1 and stat_e > 0:
            has_still = True
        return has_motion, has_still, (has_motion or has_still)

    def update(self, reading, now=None):
        if now is None:
            now = time.monotonic()
        state = reading.get('target_state')
        if state is None:
            state = STATES.index(reading['presence']) if reading.get('presence') in STATES else 0
        mov_e = reading.get('moving_energy_score') or 0
        stat_e = reading.get('stationary_energy_score') or 0

        if state == 0 and mov_e == 0:
            self.baseline_stationary_energy = (0.92 * self.baseline_stationary_energy) + (0.08 * stat_e)

        self.samples.append((now, dict(reading)))
        cutoff = now - self.window_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

        return self.evaluate(now=now)

    def evaluate(self, now=None, fallback_reading=None):
        if now is None:
            now = time.monotonic()
        cutoff = now - self.window_seconds
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

        window = list(self.samples)
        if not window and fallback_reading is not None:
            window = [(now, dict(fallback_reading))]
        if not window:
            return {
                'presence_confirmed': False,
                'presence_state': 'UNKNOWN',
                'activity_level': 0.0,
                'range_estimate_cm': None,
                'range_trend': 'UNKNOWN',
                'presence_confidence': 0.0,
                'transition_reason': 'No valid radar frames in sliding window.',
                'gate_profile': self._build_gate_profile([], None),
                'window_summary': {
                    'window_seconds': self.window_seconds,
                    'frame_count': 0,
                    'frame_rate_hz': 0.0,
                    'credible_frame_ratio': 0.0,
                    'smoothed_moving_energy': 0.0,
                    'smoothed_stationary_energy': 0.0,
                    'baseline_stationary_energy': round(self.baseline_stationary_energy, 1),
                    'gate_source': 'window_distance_bands',
                },
            }

        recent_cutoff = now - self.recent_seconds
        recent = [(t, r) for t, r in window if t >= recent_cutoff] or window[-min(len(window), 5):]

        short_cutoff = now - 1.2
        short_recent = [r for t, r in recent if t >= short_cutoff] or [r for _, r in recent[-min(len(recent), 4):]]
        mov_energies = [r.get('moving_energy_score') or 0 for r in short_recent]
        stat_energies = [r.get('stationary_energy_score') or 0 for _, r in recent]
        smoothed_mov = round(statistics.fmean(mov_energies), 1)
        smoothed_stat = round(statistics.fmean(stat_energies), 1)

        motion_flags = []
        still_flags = []
        credible_flags = []
        recent_distances = []
        for _, r in recent:
            has_m, has_s, cred = self._is_credible(r)
            motion_flags.append(has_m)
            still_flags.append(has_s)
            credible_flags.append(cred)
            if cred:
                d = _frame_distance_cm(r)
                if d is not None:
                    recent_distances.append(d)

        all_credible_distances = []
        for t, r in window:
            _, _, cred = self._is_credible(r)
            if cred:
                d = _frame_distance_cm(r)
                if d is not None:
                    all_credible_distances.append((t, d))

        credible_count = sum(credible_flags)
        motion_count = sum(motion_flags)
        credible_ratio = credible_count / len(recent)

        range_estimate_cm = None
        if recent_distances:
            range_estimate_cm = round(statistics.median(recent_distances))
            self.last_range_cm = range_estimate_cm
        elif self.confirmed and self.last_range_cm is not None:
            range_estimate_cm = self.last_range_cm

        range_trend = 'UNKNOWN'
        if len(all_credible_distances) >= 4 and (all_credible_distances[-1][0] - all_credible_distances[0][0]) >= 0.6:
            mid = len(all_credible_distances) // 2
            first_med = statistics.median(d for _, d in all_credible_distances[:mid])
            second_med = statistics.median(d for _, d in all_credible_distances[mid:])
            delta = second_med - first_med
            if delta <= -15:
                range_trend = 'CLOSER'
            elif delta >= 15:
                range_trend = 'FARTHER'
            else:
                range_trend = 'STABLE'
        elif len(all_credible_distances) >= 2:
            range_trend = 'STABLE'

        single_frame_bootstrap = len(window) < self.min_confirm_frames
        required_frames = 1 if single_frame_bootstrap else min(self.min_confirm_frames, len(recent))
        entry_confirmed = credible_count >= required_frames and (single_frame_bootstrap or credible_ratio >= 0.45)

        if entry_confirmed:
            self.confirmed = True
            self.last_confirmed_at = now
            if motion_count >= max(1, required_frames - 1) or smoothed_mov >= 18.0:
                self.state = 'PRESENT_ACTIVE'
                reason = (f'Confirmed active movement ({credible_count}/{len(recent)} recent frames, '
                          f'smoothed motion energy {smoothed_mov}/100'
                          + (f' at ~{range_estimate_cm} cm).' if range_estimate_cm else ').'))
            else:
                self.state = 'PRESENT_STILL'
                reason = (f'Confirmed stationary presence ({credible_count}/{len(recent)} recent frames, '
                          f'smoothed stationary energy {smoothed_stat}/100'
                          + (f' at ~{range_estimate_cm} cm).' if range_estimate_cm else ').'))
        elif self.confirmed and self.last_confirmed_at is not None and (now - self.last_confirmed_at) <= self.hold_seconds:
            hold_elapsed = now - self.last_confirmed_at
            if credible_count >= 1 and smoothed_stat >= max(18.0, self.baseline_stationary_energy + 8.0):
                self.state = 'PRESENT_STILL'
                self.last_confirmed_at = now
                reason = (f'Sustained still occupancy above noise floor ({smoothed_stat}/100 vs '
                          f'{round(self.baseline_stationary_energy, 1)} baseline).')
            elif range_trend == 'FARTHER' or hold_elapsed >= 1.2:
                self.state = 'DEPARTING_CANDIDATE'
                reason = (f'Signal weakening; holding presence for {round(self.hold_seconds - hold_elapsed, 1)}s '
                          f'absence debounce before clearing.')
            else:
                self.state = 'PRESENT_STILL'
                reason = (f'Holding still presence across brief signal dip '
                          f'({round(hold_elapsed, 1)}s / {self.hold_seconds}s hold).')
        else:
            self.confirmed = False
            if credible_count >= 1:
                self.state = 'APPROACHING_CANDIDATE'
                reason = (f'Unconfirmed candidate detection ({credible_count}/{self.min_confirm_frames} '
                          f'frames needed to confirm entry; filtering transient clutter).')
            else:
                self.state = 'EMPTY'
                latest_raw = window[-1][1].get('presence', 'none')
                if latest_raw != 'none':
                    reason = (f'Raw {latest_raw} frame filtered as sub-threshold clutter '
                              f'(stationary energy {smoothed_stat}/100 vs {round(self.baseline_stationary_energy, 1)} floor).')
                else:
                    reason = (f'No target detected across {len(recent)} recent frames '
                              f'(baseline stationary floor {round(self.baseline_stationary_energy, 1)}/100).')

        if self.state == 'PRESENT_ACTIVE':
            activity_level = round(min(1.0, max(0.15, smoothed_mov / 85.0)), 2)
        elif self.state in ('PRESENT_STILL', 'DEPARTING_CANDIDATE'):
            activity_level = round(min(0.25, max(0.04, smoothed_mov / 100.0 + 0.05)), 2)
        elif self.state == 'APPROACHING_CANDIDATE':
            activity_level = round(min(0.5, max(0.05, smoothed_mov / 100.0)), 2)
        else:
            activity_level = 0.0

        stat_margin = max(0.0, (smoothed_stat - self.baseline_stationary_energy) / max(1.0, 100.0 - self.baseline_stationary_energy))
        mov_norm = min(1.0, smoothed_mov / 80.0)
        energy_factor = max(mov_norm, stat_margin)
        raw_conf = (0.50 * credible_ratio) + (0.35 * energy_factor) + (0.15 if self.confirmed else 0.0)
        if not self.confirmed and self.state == 'EMPTY':
            raw_conf = min(0.12, raw_conf)
        presence_confidence = round(min(1.0, max(0.0, raw_conf)), 2)

        span = max(0.1, window[-1][0] - window[0][0]) if len(window) > 1 else 0.0
        frame_rate_hz = round((len(window) - 1) / span, 1) if len(window) > 1 else 0.0
        gate_profile, gate_source = self._build_gate_profile(recent, range_estimate_cm)

        return {
            'presence_confirmed': self.confirmed,
            'presence_state': self.state,
            'activity_level': activity_level,
            'range_estimate_cm': range_estimate_cm,
            'range_trend': range_trend,
            'presence_confidence': presence_confidence,
            'transition_reason': reason,
            'gate_profile': gate_profile,
            'window_summary': {
                'window_seconds': self.window_seconds,
                'frame_count': len(window),
                'recent_frame_count': len(recent),
                'frame_rate_hz': frame_rate_hz,
                'credible_frame_ratio': round(credible_ratio, 2),
                'smoothed_moving_energy': smoothed_mov,
                'smoothed_stationary_energy': smoothed_stat,
                'baseline_stationary_energy': round(self.baseline_stationary_energy, 1),
                'gate_source': gate_source,
            },
        }

    def _build_gate_profile(self, recent, range_estimate_cm):
        eng_mov = [r.get('moving_gate_energy') for _, r in recent if isinstance(r.get('moving_gate_energy'), list)]
        eng_stat = [r.get('stationary_gate_energy') for _, r in recent if isinstance(r.get('stationary_gate_energy'), list)]
        active_gate = (min(GATE_COUNT - 1, max(0, int(range_estimate_cm) // GATE_WIDTH_CM))
                       if isinstance(range_estimate_cm, (int, float)) and range_estimate_cm >= 0 else None)

        gates = []
        if eng_mov and eng_stat:
            gate_source = 'engineering'
            for g in range(GATE_COUNT):
                m_vals = [arr[g] for arr in eng_mov if g < len(arr)]
                s_vals = [arr[g] for arr in eng_stat if g < len(arr)]
                gates.append({
                    'gate': g,
                    'label': f'G{g}',
                    'range_m': f'{g * 0.75:.2f}–{(g + 1) * 0.75:.2f} m',
                    'min_cm': g * GATE_WIDTH_CM,
                    'max_cm': (g + 1) * GATE_WIDTH_CM,
                    'moving_energy': round(statistics.fmean(m_vals)) if m_vals else 0,
                    'stationary_energy': round(statistics.fmean(s_vals)) if s_vals else 0,
                    'active': g == active_gate,
                })
            return gates, gate_source

        gate_source = 'window_distance_bands'
        mov_buckets = {g: [] for g in range(GATE_COUNT)}
        stat_buckets = {g: [] for g in range(GATE_COUNT)}
        for _, r in recent:
            md = r.get('moving_distance_cm')
            if md is None:
                md = r.get('raw_moving_distance_cm')
            me = r.get('moving_energy_score') or 0
            if isinstance(md, (int, float)) and md > 0 and me > 0:
                g = min(GATE_COUNT - 1, max(0, int(md) // GATE_WIDTH_CM))
                mov_buckets[g].append(me)
            sd = r.get('stationary_distance_cm')
            if sd is None:
                sd = r.get('raw_stationary_distance_cm') or r.get('raw_detection_distance_cm')
            se = r.get('stationary_energy_score') or 0
            if isinstance(sd, (int, float)) and sd > 0 and se > 0:
                g = min(GATE_COUNT - 1, max(0, int(sd) // GATE_WIDTH_CM))
                stat_buckets[g].append(se)

        for g in range(GATE_COUNT):
            gates.append({
                'gate': g,
                'label': f'G{g}',
                'range_m': f'{g * 0.75:.2f}–{(g + 1) * 0.75:.2f} m',
                'min_cm': g * GATE_WIDTH_CM,
                'max_cm': (g + 1) * GATE_WIDTH_CM,
                'moving_energy': round(statistics.fmean(mov_buckets[g])) if mov_buckets[g] else 0,
                'stationary_energy': round(statistics.fmean(stat_buckets[g])) if stat_buckets[g] else 0,
                'active': g == active_gate,
            })
        return gates, gate_source


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
        self.estimator = PresenceEstimator()

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
                with serial.Serial(str(port), 256000, bytesize=serial.EIGHTBITS,
                                   parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                   timeout=0.2, exclusive=True, xonxoff=False,
                                   rtscts=False, dsrdtr=False) as connection:
                    parser = Parser()
                    with self.lock:
                        self.connected, self.mapping, self.error = True, mapping, None
                        self.latest, self.received_at = None, None
                        self.estimator.reset()
                    while not self.stop_event.is_set():
                        data = connection.read(256)
                        for reading in parser.feed(data):
                            now = time.monotonic()
                            reading['timestamp'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
                            with self.lock:
                                derived = self.estimator.update(reading, now=now)
                                reading.update(derived)
                                self.latest = reading
                                self.received_at = now
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
            now = time.monotonic()
            age = None if self.received_at is None else max(0.0, now - self.received_at)
            fresh = self.connected and age is not None and age <= self.stale_after
            age_ms = None if age is None else round(age * 1000)
            payload = {
                'sensor': 'LD2410C',
                'serial_connected': self.connected,
                'serial_port': '/dev/serial0',
                'serial_mapping': self.mapping,
                'fresh': fresh,
                'sensor_healthy': fresh,
                'age_seconds': age,
                'time_since_valid_report_ms': age_ms,
                'stale_after_seconds': self.stale_after,
                'timestamp': self.latest['timestamp'] if self.latest else None,
                'valid_frames': self.valid_frames,
                'malformed_frames': self.malformed_frames,
            }
            if fresh:
                payload.update(self.latest)
                if 'presence_state' not in payload:
                    payload.update(self.estimator.evaluate(now=now, fallback_reading=self.latest))
                payload['sensor_healthy'] = True
                payload['time_since_valid_report_ms'] = age_ms
            if not fresh:
                payload['status'] = 'unavailable'
                payload['presence_state'] = 'UNKNOWN'
                payload['presence_confirmed'] = False
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
