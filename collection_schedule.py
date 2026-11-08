"""Server-owned sensor sampling and optional periodic microphone captures."""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone


class CollectionSchedule:
    def __init__(self, collect_sensors, record, sensor_interval=10, recording_interval=120,
                 *, collect_light=None, light_interval=1, cleanup_recordings=None, cleanup_interval=60):
        self.collect_sensors, self.record = collect_sensors, record
        self.sensor_interval, self.recording_interval = sensor_interval, recording_interval
        self.collect_light, self.light_interval = collect_light, light_interval
        self.cleanup_recordings, self.cleanup_interval = cleanup_recordings, cleanup_interval
        self.condition = threading.Condition()
        self.stopped = False
        self.enabled = False
        self.recording = False
        self.next_run = None
        self.last_error = None
        self.threads = []

    def start(self):
        with self.condition:
            if self.threads or self.stopped:
                return
            workers = [(self._sensors, "sensor-sampling"), (self._microphone, "microphone-schedule")]
            if self.collect_light is not None:
                workers.append((self._light, "lux-sampling"))
            if self.cleanup_recordings is not None:
                workers.append((self._cleanup, "recording-retention"))
            self.threads = [threading.Thread(target=target, daemon=True, name=name)
                            for target, name in workers]
            for thread in self.threads:
                thread.start()

    def set_recording(self, enabled):
        with self.condition:
            if self.stopped:
                raise RuntimeError("Collection scheduler has stopped.")
            if enabled and not self.enabled:
                self.next_run = time.monotonic()
                self.last_error = None
            self.enabled = enabled
            if not enabled:
                self.next_run = None
            self.condition.notify_all()
        return self.status()

    def status(self):
        with self.condition:
            next_at = None
            if self.enabled and self.next_run is not None:
                next_at = (datetime.now(timezone.utc)+timedelta(seconds=max(0,self.next_run-time.monotonic()))).isoformat().replace("+00:00", "Z")
            return {"enabled": self.enabled, "recording": self.recording, "next_recording_at": next_at,
                    "interval_seconds": self.recording_interval, "duration_seconds": 5,
                    "sensor_interval_seconds": self.sensor_interval,
                    "light_interval_seconds": self.light_interval if self.collect_light is not None else self.sensor_interval,
                    "error": self.last_error}

    def stop(self):
        with self.condition:
            self.stopped, self.enabled, self.next_run = True, False, None
            self.condition.notify_all()
        for thread in self.threads:
            thread.join(timeout=40)

    def _wait_until(self, deadline):
        with self.condition:
            while not self.stopped and time.monotonic() < deadline:
                self.condition.wait(deadline-time.monotonic())
            return not self.stopped

    def _sensors(self):
        self._sample(self.collect_sensors, self.sensor_interval, "sensor")

    def _light(self):
        self._sample(self.collect_light, self.light_interval, "lux")

    def _sample(self, collect, interval, label):
        deadline = time.monotonic()
        while self._wait_until(deadline):
            try:
                collect()
            except Exception:
                logging.getLogger(__name__).exception("Scheduled %s collection failed", label)
            deadline += interval
            if deadline <= time.monotonic():
                deadline = time.monotonic()+interval

    def _cleanup(self):
        self._sample(self.cleanup_recordings, self.cleanup_interval, "recording retention")

    def _microphone(self):
        while True:
            with self.condition:
                while not self.stopped:
                    if not self.enabled:
                        self.condition.wait()
                    elif self.next_run > time.monotonic():
                        self.condition.wait(self.next_run-time.monotonic())
                    else:
                        break
                if self.stopped:
                    return
                self.recording = True
                self.next_run = time.monotonic()+self.recording_interval
            try:
                self.record()
                with self.condition:
                    self.last_error = None
            except Exception as exc:
                with self.condition:
                    self.last_error = str(exc)
                logging.getLogger(__name__).warning("Scheduled microphone capture failed: %s", exc)
            finally:
                with self.condition:
                    self.recording = False
                    if self.enabled and self.next_run <= time.monotonic():
                        self.next_run = time.monotonic()+self.recording_interval
                    self.condition.notify_all()
