import threading
import time
import unittest
from unittest.mock import patch

from collection_schedule import CollectionSchedule


class ScheduleTests(unittest.TestCase):
    def test_retention_runs_without_microphone_and_stops(self):
        cleaned = threading.Event()
        captured = []
        scheduler = CollectionSchedule(lambda: None, lambda: captured.append(1),
                                       cleanup_recordings=cleaned.set, cleanup_interval=.02)
        self.addCleanup(scheduler.stop)
        scheduler.start()
        self.assertTrue(cleaned.wait(1))
        scheduler.stop()
        self.assertEqual(captured, [])
        self.assertTrue(all(not thread.is_alive() for thread in scheduler.threads))

    def test_sensors_repeat_without_starting_microphone(self):
        collected, captured = [], []
        enough = threading.Event()
        def collect():
            collected.append(time.monotonic())
            if len(collected) == 3:
                enough.set()
        scheduler = CollectionSchedule(collect, lambda: captured.append(1), sensor_interval=.03)
        self.addCleanup(scheduler.stop)
        scheduler.start()
        self.assertTrue(enough.wait(1))
        scheduler.stop()
        self.assertEqual(captured, [])
        self.assertTrue(all(second-first >= .02 for first, second in zip(collected, collected[1:])))

    def test_lux_uses_one_second_deadlines_and_recovers_after_exception(self):
        now = [0]
        deadlines, collected = [], []
        def collect():
            collected.append(now[0])
            if len(collected) == 2:
                raise OSError("Lux disconnected")
        scheduler = CollectionSchedule(lambda: None, lambda: None, collect_light=collect)
        def wait(deadline):
            deadlines.append(deadline)
            now[0] = deadline
            return len(deadlines) <= 4
        scheduler._wait_until = wait
        with patch('collection_schedule.time.monotonic', side_effect=lambda: now[0]):
            with self.assertLogs('collection_schedule', level='ERROR'):
                scheduler._light()
        self.assertEqual(collected, [0, 1, 2, 3])
        self.assertEqual(deadlines, [0, 1, 2, 3, 4])
        self.assertEqual(scheduler.status()['light_interval_seconds'], 1)
        self.assertEqual(scheduler.status()['sensor_interval_seconds'], 10)
        self.assertEqual(scheduler.status()['interval_seconds'], 120)
        self.assertFalse(scheduler.status()['enabled'])

    def test_lux_overrun_skips_missed_ticks_instead_of_burst_reading(self):
        now = [0]
        collected = []
        def collect():
            collected.append(now[0])
            now[0] += 2
        scheduler = CollectionSchedule(lambda: None, lambda: None, collect_light=collect)
        def wait(deadline):
            now[0] = deadline
            return len(collected) < 3
        scheduler._wait_until = wait
        with patch('collection_schedule.time.monotonic', side_effect=lambda: now[0]):
            scheduler._light()
        self.assertEqual(collected, [0, 3, 6])

    def test_lux_sampling_is_independent_of_blocked_presence_and_stops_cleanly(self):
        entered, release, enough = threading.Event(), threading.Event(), threading.Event()
        collected, captured = [], []
        def presence():
            entered.set()
            release.wait(1)
        def lux():
            collected.append(time.monotonic())
            if len(collected) >= 3:
                enough.set()
        scheduler = CollectionSchedule(presence, lambda: captured.append(1),
                                       collect_light=lux, light_interval=.02)
        try:
            scheduler.start()
            scheduler.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(enough.wait(1))
            self.assertFalse(release.is_set())
            self.assertEqual(len(scheduler.threads), 3)
            self.assertEqual(captured, [])
        finally:
            release.set()
            scheduler.stop()
        self.assertTrue(all(not thread.is_alive() for thread in scheduler.threads))
        count = len(collected)
        time.sleep(.05)
        self.assertEqual(len(collected), count)

    def test_start_records_immediately_repeats_and_stop_prevents_future_captures(self):
        captured = []
        enough = threading.Event()
        def record():
            captured.append(time.monotonic())
            if len(captured) == 2:
                enough.set()
        scheduler = CollectionSchedule(lambda: None, record, recording_interval=.08)
        self.addCleanup(scheduler.stop)
        scheduler.start()
        scheduler.set_recording(True)
        scheduler.set_recording(True)
        self.assertTrue(enough.wait(1))
        state = scheduler.set_recording(False)
        count = len(captured)
        time.sleep(.12)
        self.assertEqual(len(captured), count)
        self.assertFalse(state["enabled"])
        self.assertIsNone(state["next_recording_at"])
        self.assertGreaterEqual(captured[1]-captured[0], .06)

    def test_stop_during_capture_finishes_capture_and_does_not_repeat(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def record():
            calls.append(1)
            started.set()
            release.wait(1)
            finished.set()
        scheduler = CollectionSchedule(lambda: None, record, recording_interval=.03)
        self.addCleanup(scheduler.stop)
        self.addCleanup(release.set)
        scheduler.start()
        scheduler.set_recording(True)
        self.assertTrue(started.wait(1))
        state = scheduler.set_recording(False)
        self.assertTrue(state["recording"])
        release.set()
        self.assertTrue(finished.wait(1))
        time.sleep(.08)
        self.assertEqual(calls, [1])

    def test_hardware_failure_is_visible_and_does_not_stop_sensor_loop(self):
        failed = threading.Event()
        def record():
            failed.set()
            raise RuntimeError("Microphone disconnected")
        scheduler = CollectionSchedule(lambda: None, record)
        self.addCleanup(scheduler.stop)
        with self.assertLogs("collection_schedule", level="WARNING"):
            scheduler.start()
            scheduler.set_recording(True)
            self.assertTrue(failed.wait(1))
            scheduler.stop()
        self.assertIn("disconnected", scheduler.status()["error"])
