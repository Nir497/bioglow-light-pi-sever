import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from adaptive_lighting import (AdaptiveConfig, AdaptivePolicy, calculate_brightness,
                               distance_target, nearest_distance, migration_from_snapshot)
from lighting_control import LightingControl
from birdcast_scraper import parse_snapshot, recorded_time_utc
from wled_serial import WLEDSerial


class CalculationTests(unittest.TestCase):
    def test_daylight_and_no_presence(self):
        self.assertEqual(calculate_brightness(False, True, 1, 'low'), 0)
        self.assertEqual(calculate_brightness(True, False, 1, 'low'), 0)

    def test_distance_table(self):
        for distance, expected in ((0, 60), (1, 50), (2, 40), (3, 30), (4, 20), (5, 10), (5.01, 0)):
            with self.subTest(distance=distance):
                self.assertEqual(calculate_brightness(True, True, distance, 'low'), expected)

    def test_invalid_distances(self):
        for distance in (None, -1, float('nan'), float('inf'), '1', True):
            self.assertEqual(calculate_brightness(True, True, distance, 'low'), 0)

    def test_migration_caps_never_increase_distance_request(self):
        for level, cap in (('low', 60), ('medium', 40), ('high', 20), ('very_high', 10), ('unknown', 10)):
            for distance in (0, 1, 3, 4, 5):
                self.assertEqual(calculate_brightness(True, True, distance, level), min(distance_target(distance), cap))

    def test_config_validation_and_load(self):
        for settings in ({'max_distance_m': 0}, {'dark_lux': 26}, {'max_brightness': 101},
                         {'min_brightness': 90}, {'presence_timeout_seconds': -1},
                         {'max_distance_m': float('nan')}, {'dark_lux': True}):
            with self.assertRaises(ValueError):
                AdaptiveConfig(**settings)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'adaptive-lighting.json'
            path.write_text('{"max_distance_m": 10, "max_brightness": 80}')
            config = AdaptiveConfig.load(tmp)
            self.assertEqual(distance_target(5, config), 45)
            path.write_text('{"unknown": 1}')
            with self.assertRaises(TypeError):
                AdaptiveConfig.load(tmp)

    def test_nearest_active_channel_and_bounds(self):
        reading = dict(presence='both', moving_distance_cm=400, stationary_distance_cm=200)
        self.assertEqual(nearest_distance(reading, 5), 2)
        reading['presence'] = 'moving'
        self.assertEqual(nearest_distance(reading, 5), 4)
        reading.update(presence='stationary', stationary_distance_cm=500)
        self.assertEqual(nearest_distance(reading, 5), 5)
        reading['stationary_distance_cm'] = 501
        self.assertIsNone(nearest_distance(reading, 5))
        reading.update(presence='both', moving_distance_cm=float('nan'))
        self.assertIsNone(nearest_distance(reading, 5))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = AdaptivePolicy()

    def step(self, now, distance=1, level='low', dark=True, valid=True):
        return self.policy.step(now, dark, valid, distance, level)

    def test_approaching_and_departing_continuous_targets(self):
        targets = [self.step(t / 10, d)['final_target_percent'] for t, d in enumerate((5, 4, 3, 2, 1))]
        self.assertEqual(targets, [10, 20, 30, 40, 50])
        targets = [self.step(1 + t / 10, d)['final_target_percent'] for t, d in enumerate((1, 2, 3, 4, 5))]
        self.assertEqual(targets, [50, 40, 30, 20, 10])

    def test_stationary_presence_never_expires(self):
        for tick in range(200):
            result = self.step(tick / 10, distance=3)
        self.assertEqual(result['lighting_state'], 'tracking')
        self.assertAlmostEqual(result['output_percent'], 30)
        self.assertIsNone(result['presence_timeout_remaining_seconds'])

    def test_loss_hold_return_cancel_and_fade(self):
        self.step(0)
        self.step(5)
        held = self.policy.output
        result = self.step(6, None)
        self.assertEqual(result['lighting_state'], 'presence_hold')
        self.assertAlmostEqual(self.policy.output, held)
        self.assertEqual(result['presence_timeout_remaining_seconds'], 10)
        self.step(15, None)
        result = self.step(15.5, 2)
        self.assertIsNone(result['presence_timeout_remaining_seconds'])
        self.step(16, None)
        self.assertEqual(self.step(25.9, None)['lighting_state'], 'presence_hold')
        result = self.step(26, None)
        self.assertEqual(result['final_target_percent'], 0)
        self.assertGreater(result['output_percent'], 0)
        self.assertEqual(self.step(30, None)['output_percent'], 0)

    def test_cap_decrease_is_immediate_during_hold_and_fade(self):
        self.step(0)
        self.step(5)
        self.assertGreater(self.policy.output, 40)
        self.assertLessEqual(self.step(5.1, level='high')['output_percent'], 20)
        self.assertLessEqual(self.step(6, None, 'very_high')['output_percent'], 10)
        self.assertLessEqual(self.step(6.1, None, 'low')['output_percent'], 10)
        self.assertLessEqual(self.step(17, None, 'very_high')['output_percent'], 10)

    def test_daylight_and_sensor_failure_bypass_fade_and_hold(self):
        for overrides in ({'dark': False}, {'valid': False}):
            self.step(0)
            self.step(5)
            self.step(6, None)
            result = self.step(6.1, **overrides)
            self.assertEqual(result['output_percent'], 0)
            self.assertEqual(result['final_target_percent'], 0)
            self.assertEqual(self.policy.held, 0)

    def test_time_aware_smoothing(self):
        # Same elapsed time produces the same exponential response at variable rates.
        self.step(0)
        for tick in range(1, 11):
            self.step(tick / 10)
        other = AdaptivePolicy()
        other.step(0, True, True, 1, 'low')
        other.step(1, True, True, 1, 'low')
        self.assertAlmostEqual(self.policy.output, other.output)
        self.assertLess(self.policy.output, 50)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        stamp = self.now.isoformat()
        self.snapshot = dict(status='ok', region={'code': 'US-CA'}, retrieved_at=stamp,
                             live_feed=True, migration_level='high', metrics={'recorded_at_utc': stamp})

    def test_fresh_documented_high(self):
        self.assertEqual(migration_from_snapshot(self.snapshot, AdaptiveConfig(), self.now)[0], 'high')

    def test_stale_invalid_future_historical_wrong_region_unknown(self):
        variants = [None, {}, {**self.snapshot, 'live_feed': False},
                    {**self.snapshot, 'region': {'code': 'US-NY'}},
                    {**self.snapshot, 'migration_level': 'low'},
                    {**self.snapshot, 'retrieved_at': (self.now + timedelta(seconds=1)).isoformat()},
                    {**self.snapshot, 'metrics': {'recorded_at_utc': (self.now - timedelta(hours=2)).isoformat()}},
                    {**self.snapshot, 'metrics': {'recorded_at_utc': 'yesterday'}}]
        for snapshot in variants:
            self.assertEqual(migration_from_snapshot(snapshot, AdaptiveConfig(), self.now)[0], 'unknown')

    def test_parser_documents_only_high_no_count_thresholds(self):
        html = '''California - Migration Dashboard - BirdCast
        LIVE DATA FEED Last updated 5 min. ago Live migration traffic
        1,000,000 Birds now in flight (est.) High
        Direction: S Speed: 9 mph Altitude: 1000 ft
        Recorded: Fri, Nov 6, 2026, 5:50 PM PST
        Not all birds may fully cross a region'''
        snapshot = parse_snapshot(html, 'US-CA', 'https://dashboard.birdcast.org/region/US-CA', self.now.isoformat())
        self.assertEqual(snapshot['migration_level'], 'high')
        self.assertTrue(snapshot['live_feed'])
        self.assertEqual(snapshot['metrics']['recorded_at_utc'], '2026-11-07T01:50:00Z')
        for count in ('10', '1000000000'):
            snapshot = parse_snapshot(html.replace('1,000,000', count).replace('(est.) High', '(est.)'),
                                      'US-CA', 'source', self.now.isoformat())
            self.assertEqual(snapshot['migration_level'], 'unknown')
        self.assertIsNone(recorded_time_utc('unrecognized'))


class IntegratedLightingTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.transport = MagicMock()
        self.transport.send.return_value = {}
        self.light = LightingControl(transport=self.transport, clock=lambda: self.now)
        self.addCleanup(self.light.stop)
        # Exercise all policy levels without pretending these are API classifications.
        self.migration = patch('lighting_control.migration_from_snapshot', return_value=('low', 0, None))
        self.migration.start()
        self.addCleanup(self.migration.stop)
        self.light.update(0)

    def read(self, distance=100, presence='moving', **extra):
        reading = dict(fresh=True, presence=presence, moving_distance_cm=distance,
                       stationary_distance_cm=distance, age_seconds=0, stale_after_seconds=2)
        reading.update(extra)
        self.light.update_distance(200, reading)
        return self.light.status()

    def settle(self, distance=100, presence='moving'):
        self.read(distance, presence)
        for _ in range(30):
            self.now += 0.1
            self.light.update(0)
            self.read(distance, presence)
        return self.light.status()

    def test_current_channels_update_without_range_estimate(self):
        for d, target in ((100, 50), (300, 30), (500, 10), (501, 0)):
            state = self.read(d)
            if d <= 500:
                self.assertEqual(state['person_distance_m'], d / 100)
                self.assertEqual(state['distance_target_percent'], target)
                self.assertEqual(state['final_target_percent'], target)
        other = LightingControl(transport=self.transport)
        other.update(0)
        other.update_distance(200, dict(fresh=True, presence='moving', moving_distance_cm=501))
        self.assertEqual(other.status()['brightness'], 0)
        other.stop()

    def test_stationary_and_nearest_channels(self):
        self.assertGreater(self.settle(300, 'stationary')['brightness'], 0)
        state = self.read(400, 'both', stationary_distance_cm=100)
        self.assertEqual(state['person_distance_m'], 1)
        self.assertEqual(state['final_target_percent'], 50)

    def test_default_unknown_cap(self):
        self.migration.stop()
        self.assertEqual(self.settle()['migration_cap_percent'], 10)
        self.assertLessEqual(self.light.status()['brightness'], 25)

    def test_hold_then_return_and_fade(self):
        before = self.settle()['brightness']
        self.now += .1
        state = self.read(None, 'none')
        self.assertEqual(state['brightness'], before)
        self.assertEqual(state['lighting_state'], 'presence_hold')
        self.now += 1
        self.assertEqual(self.read()['lighting_state'], 'tracking')
        self.now += .1
        self.read(None, 'none')
        self.now += 10
        self.light.update(0)
        state = self.read(None, 'none')
        self.assertEqual(state['final_target_percent'], 0)

    def test_hysteresis_filter_daylight_and_invalid_recovery(self):
        self.settle()
        for lux in (15, 20, 25, 20):
            self.now += 1
            self.light.update(lux)
            self.assertEqual(self.light.status()['ambient_state'], 'dark')
        self.light.update(25.01)
        self.assertEqual(self.light.status()['brightness'], 0)
        for lux in (25, 20, 15):
            self.now += 1
            self.light.update(lux)
            self.assertEqual(self.light.status()['ambient_state'], 'light')
        self.light.update(None)
        self.light.update(20)
        self.assertEqual(self.read()['brightness'], 0)
        self.now += 1
        self.light.update(0)
        self.assertEqual(self.light.status()['brightness'], 0, 'Wait for new radar')
        self.assertGreater(self.read()['brightness'], 0)

    def test_ambient_stale_status_passive_and_color_cannot_bypass(self):
        self.settle()
        self.now += 3.01
        count = self.transport.send.call_count
        self.assertEqual(self.light.status()['ambient_state'], 'unknown')
        self.assertEqual(self.transport.send.call_count, count)
        self.assertEqual(self.light.configure(color='#123456')['brightness'], 0)

    def test_bad_radar_immediate_off_and_no_stale_hold_resurrection(self):
        for bad in ({'fresh': False}, {'age_seconds': 3}, {'age_seconds': float('nan')},
                    {'serial_connected': False}, {'presence': 'invalid'},
                    {'moving_distance_cm': float('nan')}, {'moving_distance_cm': -1}):
            self.settle()
            state = self.read(**bad)
            self.assertEqual(state['brightness'], 0, bad)
            self.assertIn('sensor_error', state)
            self.assertEqual(self.read(None, 'none')['brightness'], 0)

    def test_command_watchdog_heartbeat_and_shutdown(self):
        self.settle()
        self.assertEqual(self.transport.send.call_args.args[0]['bioglow'], {'watchdog_ms': 3000})
        count = self.transport.send.call_count
        self.now += 1.1
        self.light.update(0)
        self.read()
        self.assertGreater(self.transport.send.call_count, count)
        self.light.stop()
        self.assertFalse(self.transport.send.call_args.args[0]['on'])

    def test_slow_controller_does_not_block_sampling_and_queue_is_latest(self):
        entered, release, sampled = threading.Event(), threading.Event(), threading.Event()
        commands = []
        count = [0]
        def send(command):
            commands.append(command)
            if command['on'] and not entered.is_set():
                entered.set()
                release.wait(3)
            return {}
        self.transport.send.side_effect = send
        light = LightingControl(transport=self.transport, background=True)
        light.update(0)
        def read():
            count[0] += 1
            if count[0] > 3:
                sampled.set()
            d = 100 if count[0] == 1 else 300
            return 200, dict(fresh=True, presence='moving', moving_distance_cm=d)
        try:
            light.start_distance_sampling(read)
            self.assertTrue(entered.wait(1))
            self.assertTrue(sampled.wait(1))
            self.assertEqual(light.status()['person_distance_m'], 3)
            release.set()
            with light._condition:
                self.assertTrue(light._condition.wait_for(lambda: light._state['status'] == 'ok', timeout=2))
            light.update(100)
            with light._condition:
                self.assertTrue(light._condition.wait_for(lambda: light._state['brightness'] == 0, timeout=2))
        finally:
            release.set()
            light.stop()
        self.assertFalse(commands[-1]['on'])

    def test_output_worker_watchdogs_even_if_sampler_stops(self):
        for ambient_stale in (False, True):
            clock = [0]
            light = LightingControl(transport=self.transport, background=True, clock=lambda: clock[0])
            light.update(0)
            try:
                light.update_distance(200, dict(fresh=True, presence='moving', moving_distance_cm=100))
                with light._condition:
                    self.assertTrue(light._condition.wait_for(lambda: bool(light._state['brightness']), timeout=1))
                    clock[0] = 4
                    if ambient_stale:
                        light._radar_sampled_at = 4
                    else:
                        light._lux_at = 4
                    light._condition.notify_all()
                    self.assertTrue(light._condition.wait_for(lambda: light._state['brightness'] == 0, timeout=2))
            finally:
                light.stop()

    def test_cap_drop_replaces_queued_output_and_requests_instant_clamp(self):
        self.settle(0)
        self.assertGreater(self.light.status()['brightness'], 100)
        with patch('lighting_control.migration_from_snapshot', return_value=('high', 0, None)):
            self.now += .1
            self.read(0)
        command = self.transport.send.call_args.args[0]
        self.assertLessEqual(command['bri'], 51)
        self.assertEqual(command['transition'], 0)
        self.assertEqual(self.light.status()['final_target_percent'], 20)

    def test_malformed_age_status_stays_json_safe_and_fails_off(self):
        self.settle()
        state = self.read(age_seconds='invalid')
        self.assertEqual(state['brightness'], 0)
        json.dumps(state, allow_nan=False)
        self.assertIsNone(state['person_distance_m'])

    def test_migration_cache_update_cannot_resurrect_stale_radar(self):
        self.settle()
        self.now += 2.1
        self.light.update_migration({})
        self.assertEqual(self.light.status()['brightness'], 0)
        self.assertIsNone(self.light.status()['person_distance_m'])

    def test_auto_does_not_require_watchdog_and_legacy_http_can_send_output(self):
        self.assertFalse(self.light.status()['controller_watchdog_required'])
        opener = MagicMock()
        opener.return_value.__enter__.return_value.status = 200
        light = LightingControl(opener=opener)
        try:
            light.update(0)
            light.update_distance(200, dict(fresh=True, presence='moving', moving_distance_cm=100))
            self.assertGreater(light.status()['brightness'], 0)
            self.assertEqual(light.status()['status'], 'ok')
            self.assertTrue(json.loads(opener.call_args.args[0].data)['on'])
        finally:
            light.stop()

    def test_failed_transport_never_claims_brightness(self):
        self.transport.send.side_effect = OSError('unplugged')
        state = self.read()
        self.assertIsNone(state['brightness'])
        self.assertEqual(state['status'], 'controller_unavailable')
        self.assertIn('unplugged', state['error'])


class ControllerWatchdogTests(unittest.TestCase):
    def test_cpp_watchdog_expiry_refresh_and_millis_rollover(self):
        source = '''#include "firmware/wled/command_watchdog.h"
#include <cassert>
int main() {
 BioGlowCommandWatchdog w; assert(!w.expired(99999));
 w.command(100,3000); assert(w.armed()); assert(!w.expired(3099));
 w.command(2000,3000); assert(!w.expired(4999)); assert(w.expired(5000));
 assert(!w.armed()); assert(!w.expired(6000));
 w.command(0xfffffff0,3000); assert(!w.expired(100)); assert(w.expired(3000));
 w.command(10,3000); w.command(20,0); assert(!w.expired(99999));
}'''
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'watchdog.cpp'
            path.write_text(source)
            binary = str(Path(tmp) / 'watchdog')
            subprocess.run(['g++', '-std=c++11', '-Wall', '-Werror', '-I', str(root), str(path), '-o', binary], check=True, capture_output=True)
            subprocess.run([binary], check=True)

    def test_stock_wled_works_and_installed_watchdog_must_arm(self):
        for capability, armed in ((False, False), (True, False), (True, True)):
            light = WLEDSerial(port='test')
            current = {'info': {'brand': 'WLED', 'arch': 'esp32', 'leds': {'count': 144}},
                       'state': {'on': False, 'bri': 0, 'seg': []}}
            if capability:
                current['info']['bioglow_watchdog'] = 1
            ack = copy.deepcopy(current)
            ack['state'].update(on=True, bri=25, bioglow={'watchdog_ms': 3000 if armed else 0},
                                seg=[dict(id=0, start=0, stop=144, grp=1, spc=0, of=0,
                                          on=True, bri=255, fx=0, pal=0, frz=False, col=[[255,214,170,0]])])
            replies = iter([current, ack, current])
            with patch.object(light, '_query', return_value=current), patch.object(light, '_read_reply', side_effect=lambda: next(replies)), patch.object(light, '_write') as write, patch('wled_serial.serial.Serial'):
                # query is mocked separately, so command ack must come first
                replies = iter([ack, current])
                params = dict(on=True, bri=25, bioglow={'watchdog_ms': 3000}, seg=[{'col': [[255,214,170,0]]}])
                if not capability or armed:
                    self.assertEqual(light.send(params)['watchdog_protocol'], 1 if capability else None)
                    command = write.call_args.args[0]
                    self.assertTrue(command['on'])
                    self.assertEqual(command['bri'], 25)
                    if capability:
                        self.assertEqual(command['bioglow']['watchdog_ms'], 3000)
                    else:
                        self.assertNotIn('bioglow', command)
                else:
                    with self.assertRaisesRegex(OSError, 'watchdog'):
                        light.send(params)
                    self.assertFalse(write.call_args.args[0]['on'])
