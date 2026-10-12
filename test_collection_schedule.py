import threading
import time
import unittest

from collection_schedule import CollectionSchedule


class ScheduleTests(unittest.TestCase):
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
