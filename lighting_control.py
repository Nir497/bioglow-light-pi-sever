"""Server-owned distance brightness and development distance control for the BioGlow fixture."""
import json
import logging
import copy
import ipaddress
import math
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict
from adaptive_lighting import AdaptiveConfig, AdaptivePolicy, nearest_distance, finite, migration_from_snapshot, distance_target, calculate_brightness
from urllib.request import Request, urlopen, build_opener, ProxyHandler


LUX_DARK_THRESHOLD = 15
LUX_LIGHT_THRESHOLD = 25
LUX_STALE_AFTER_SECONDS = 3


def distance_brightness(distance_cm):
    """Map a usable radar range to full-strip brightness independently of rendering."""
    return int(distance_target(distance_cm / 100) * 255 / 100) if finite(distance_cm) else 0


class LightingControl:
    def __init__(self, host="bioglow.local", opener=urlopen, clock=time.monotonic, background=False, transport=None, adaptive_config=None):
        self.adaptive_config = adaptive_config or AdaptiveConfig()
        self.adaptive = AdaptivePolicy(self.adaptive_config)
        self._migration_snapshot = None
        self._last_adaptive_state = None
        self._motion_session_active = False
        self.transport = transport
        self.host = os.environ.get("BIOGLOW_LIGHT_HOST", host)
        self._opener = build_opener(ProxyHandler({})).open if opener is urlopen else opener
        self._clock = clock
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending = None
        self._desired = None
        self._last_ack_at = None
        self._stopped = False
        self._background = background
        self._worker = None
        self._radar_worker = None
        self._radar_stop = threading.Event()
        self._presence_reader = None
        self.sound_reader = None
        self._sound = {"source": "unavailable", "reason": "No recording checked."}
        self._revision = 0
        self._radar_snapshot = None
        self._radar_sampled_at = None
        self._last_distance_command = None
        self._last_distance_sent_at = None
        self._address = None
        device_info = getattr(transport, "device_info", None)
        self._device_info = dict(device_info) if isinstance(device_info, dict) else {}
        self._samples = deque()
        self._lux = None
        self._lux_at = None
        self._lux_seen = False
        self._ambient_state = "unknown"
        self._auto_target = 0
        self._auto_last_distance = None
        self._auto_last_change = None
        self._state = {"name": "BioGlow", "hostname": self.host, "brightness": None,
                       "target_brightness": None, "status": "waiting", "mode": "auto",
                       "connection": "usb" if transport else "wifi",
                       "manual_brightness": 255, "color": "#ffd6aa", "on": None}

    def load_preferences(self, prefs):
        """Install accepted preferences without activating any output regime."""
        with self._lock:
            self._state.update(manual_brightness=prefs["manual_brightness"], color=prefs["color"],
                               alignment=prefs["alignment"], beam_width=prefs["beam_width"],
                               direction_offset=prefs["offset"],
                               alignment_brightness=prefs["calibration_brightness"])

    def _ambient_status(self):
        """Read-only view; callers hold the lighting lock."""
        age = None if self._lux_at is None else max(0, self._clock() - self._lux_at)
        unavailable = self._lux_seen and (age is None or age > self.adaptive_config.lux_stale_seconds)
        ambient = "unknown" if unavailable else self._ambient_state
        reason = ("lux_unavailable" if unavailable else "waiting_lux" if ambient == "unknown"
                  else "daylight" if ambient == "light" else "distance_control")
        return {"lux": self._lux, "lux_age_seconds": age, "ambient_state": ambient,
                "auto_gate_reason": reason if self._state["mode"] == "auto" else None,
                "lux_dark_threshold": self.adaptive_config.dark_lux,
                "lux_light_threshold": self.adaptive_config.bright_lux,
                "lux_stale_after_seconds": self.adaptive_config.lux_stale_seconds}

    def _lighting_state(self):
        state = {**self._state, **self._ambient_status(), "adaptive_config": asdict(self.adaptive_config),
                 "actual_brightness_percent": None if self._state["brightness"] is None else round(self._state["brightness"] / 255 * 100, 2),
                 "actual_brightness_source": "controller_acknowledgement_not_optical_measurement",
                 "controller_watchdog_required": False,
                 "controller_watchdog_status": ("supported" if self._device_info.get("watchdog_protocol") == 1
                                                else "unavailable" if self._device_info.get("firmware") else "unconfirmed")}
        if self._state.get("mode") == "direction_config":
            state["alignment"] = self._state.get("alignment", 144)
            state["beam_width"] = self._state.get("beam_width", 1)
        return state

    def _auto_allowed(self):
        return self._ambient_status()["ambient_state"] == "dark"

    def status(self):
        with self._lock:
            result = {**self._lighting_state(), "device": dict(self._device_info)}
            result["radar_loop_running"] = bool(self._radar_worker and self._radar_worker.is_alive())
            result["sample_age_seconds"] = (None if self._radar_sampled_at is None
                                            else max(0, self._clock() - self._radar_sampled_at))
            result["ack_age_seconds"] = (None if self._last_ack_at is None
                                         else max(0, self._clock() - self._last_ack_at))
            if self._state["mode"] in ("auto", "distance_test") and self._radar_snapshot is not None:
                radar = copy.deepcopy(self._radar_snapshot)
                age = self._radar_age()
                radar["result"]["age_seconds"] = age if math.isfinite(age) else None
                if age > 2:
                    radar = {"http_status": 503, "result": {
                        "fresh": False, "status": "unavailable", "sensor_healthy": False,
                        "timestamp": radar["result"].get("timestamp"), "age_seconds": age if math.isfinite(age) else None,
                        "error": {"code": "presence_unavailable", "message": "Sampled radar frame is stale."}}}
                    result.update(distance_cm=None, person_distance_m=None, presence_detected=False, led_index=None, test_state="sensor_unavailable",
                                  sensor_error=result.get("sensor_error") or "Sampled radar frame is stale; awaiting fresh sensor data.")
                result["radar_snapshot"] = radar
            return result

    def set_mode(self, mode):
        return self.configure(mode=mode)

    def configure(self, mode=None, brightness=None, color=None):
        if mode is not None and mode not in ("on", "off", "auto", "distance_test", "direction_config"):
            raise ValueError('mode must be "on", "off", "auto", "distance_test", or "direction_config"')
        if brightness is not None and (type(brightness) is not int or not 1 <= brightness <= 255):
            raise ValueError("brightness must be an integer from 1 to 255")
        if color is not None and (not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
            raise ValueError("color must be a hex RGB value such as #ff8000")
        if mode is None and brightness is None and color is None:
            raise ValueError("Provide mode, brightness, or color")
        if mode == "direction_config":
            return self.set_direction_alignment(
                self._state.get("alignment", 144),
                beam_width=self._state.get("beam_width", 1),
                offset=self._state.get("direction_offset", 0),
                brightness=brightness,
                color=color
            )
        with self._lock:
            if self._stopped:
                raise ValueError("Lighting control has stopped")
            if self._state["mode"] == "distance_test" and mode is None:
                raise ValueError("Select On, Off, or Auto before changing appearance")
            if mode is None and brightness is None and self._state["mode"] == "auto":
                self._state["color"] = color.lower()
                self._revision += 1
                self._last_distance_command = None
                self._send(self._auto_target, instant=self._auto_target == 0)
                return self._lighting_state()
            if brightness is not None:
                self._state["manual_brightness"] = brightness
            if color is not None:
                self._state["color"] = color.lower()
            if mode is None:
                mode = self._state["mode"] if brightness is None else ("off" if self._state["mode"] == "off" else "on")
            self.adaptive.reset()
            self._motion_session_active = False
            self._state["mode"] = mode
            self._revision += 1
            self._pending = None
            self._radar_snapshot = None
            self._radar_sampled_at = None
            self._auto_last_distance = None
            self._auto_last_change = None
            self._last_distance_command = None
            self._state.pop("sensor_error", None)
            self._state.pop("error", None)
            if mode in ("auto", "distance_test"):
                self._state.update(distance_cm=None, led_index=None, sample_interval_seconds=0.1,
                                   test_state="waiting", test_brightness=64, test_color="#ffa028",
                                   distance_source="range_estimate_cm", radar_timestamp=None,
                                   confirmed_led_index=None, confirmed_distance_cm=None, confirmed_radar_timestamp=None)
                if mode == "auto":
                    for key in ("test_brightness", "test_color"):
                        self._state.pop(key, None)
                self._auto_target = 0
                self._send(0, color="#ffa028" if mode == "distance_test" else None, instant=True)
                return self._lighting_state()
            for key in ("distance_cm", "led_index", "test_state", "test_brightness", "test_color", "sample_interval_seconds", "distance_source", "radar_timestamp", "confirmed_led_index",
                        "confirmed_distance_cm", "confirmed_radar_timestamp"):
                self._state.pop(key, None)
            self._send(self._state["manual_brightness"] if mode == "on" else 0)
            return self._lighting_state()

    def set_direction_alignment(self, spacing: int, beam_width: int = 1, offset: int = 0, brightness: int = 64, color: str = None):
        if type(spacing) is not int or not 1 <= spacing <= 144:
            raise ValueError("alignment spacing must be an integer from 1 to 144")
        max_beam_width = min(143, 2 * (spacing // 2) + 1)
        if type(beam_width) is not int or beam_width < 1 or beam_width > max_beam_width or beam_width % 2 == 0:
            raise ValueError(f"beam_width must be an odd integer from 1 to {max_beam_width} for alignment spacing {spacing}")
        if type(offset) is not int or not 0 <= offset < spacing:
            raise ValueError(f"offset must be an integer from 0 to {spacing - 1}")
        if brightness is not None and (type(brightness) is not int or not 1 <= brightness <= 255):
            raise ValueError("brightness must be an integer from 1 to 255")
        if color is not None and (not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
            raise ValueError("color must be a hex RGB value such as #ff8000")
        with self._lock:
            if self._stopped:
                raise ValueError("Lighting control has stopped")
            self._state["mode"] = "direction_config"
            self._state["alignment"] = spacing
            self._state["beam_width"] = beam_width
            self._state["direction_offset"] = offset
            if brightness is not None:
                self._state["alignment_brightness"] = brightness
            if color is not None:
                self._state["color"] = color.lower()
            self._revision += 1
            self._pending = None
            self._radar_snapshot = None
            self._radar_sampled_at = None
            self._auto_last_distance = None
            self._auto_last_change = None
            self._last_distance_command = None
            for key in ("distance_cm", "led_index", "test_state", "test_brightness", "test_color",
                        "sample_interval_seconds", "distance_source", "radar_timestamp",
                        "confirmed_led_index", "confirmed_distance_cm", "confirmed_radar_timestamp"):
                self._state.pop(key, None)
            target_bri = self._state.get("alignment_brightness", 64)
            chosen_color = self._state.get("color", "#ffd6aa")
            if beam_width >= spacing:
                grp = 1
                spc = 0
                of = 0
            else:
                grp = beam_width
                spc = spacing - beam_width
                of = offset
            self._send(target_bri, color=chosen_color, instant=True, grp=grp, spc=spc, of=of)
            return self._lighting_state()

    def update(self, lux):
        with self._lock:
            if self._stopped:
                return
            # A failed/stale classification cannot carry through the middle band.
            was_dark = self._auto_allowed()
            self._ambient_state = self._ambient_status()["ambient_state"]
            self._lux_seen = True
            now = self._clock()
            if finite(lux) and lux >= 0:
                elapsed = 1 if self._lux_at is None else max(0, now - self._lux_at)
                old = self._state.get("smoothed_lux") if self._ambient_state != "unknown" else None
                filtered = lux if old is None else old + (1 - math.exp(
                    -elapsed / self.adaptive_config.lux_filter_tau_seconds)) * (lux - old)
                self._lux, self._lux_at = lux, now
                self._state["smoothed_lux"] = filtered
                # Daylight shuts down immediately even before the filter settles.
                if lux > self.adaptive_config.bright_lux:
                    self._ambient_state = "light"
                elif lux < self.adaptive_config.dark_lux and filtered < self.adaptive_config.dark_lux:
                    self._ambient_state = "dark"
            else:
                self._lux = self._lux_at = None
                self._ambient_state = "unknown"
                self._state["smoothed_lux"] = None
            if not was_dark:
                self._auto_target = 0
                self._auto_last_distance = self._auto_last_change = None
            if self._state["mode"] == "auto" and (not self._auto_allowed()
                    or (not was_dark and self._state["target_brightness"])):
                self._send(0, instant=True)
            elif self._state["mode"] in ("on", "off"):
                self._send(self._state["manual_brightness"] if self._state["mode"] == "on" else 0)

    def start_distance_sampling(self, presence_reader):
        """Sample the existing UART receiver; never open a second sensor connection."""
        with self._lock:
            if self._radar_worker is not None or self._stopped:
                return
            self._presence_reader = presence_reader
            if self._state["mode"] == "auto":
                self._send(0, instant=True)
            self._radar_worker = threading.Thread(target=self._sample_radar, daemon=True,
                                                  name="distance-light-sampling")
            self._radar_worker.start()

    def _sample_radar(self):
        deadline = self._clock()
        while not self._radar_stop.is_set():
            with self._lock:
                active = self._state["mode"] in ("auto", "distance_test")
                sound_active = self._state["mode"] == "auto"
                revision = self._revision
            if active:
                try:
                    status, reading = self._presence_reader()
                    if not isinstance(reading, dict):
                        raise ValueError("Radar returned an invalid reading")
                    if sound_active and self.sound_reader and reading.get("presence") in ("moving", "stationary", "both"):
                        sound = self.sound_reader()
                        with self._lock:
                            self._sound = sound
                    self.update_distance(status, reading, revision)
                except Exception as exc:
                    # A bad frame must not silently terminate automatic control.
                    self.update_distance(503, {"error": str(exc)}, revision)
            deadline += 0.1
            now = self._clock()
            if deadline <= now:
                deadline = now + 0.1
            self._radar_stop.wait(max(0, deadline - now))

    def update_distance(self, status, reading, revision=None):
        with self._lock:
            if self._stopped or self._state["mode"] not in ("auto", "distance_test"):
                return
            if revision is not None and revision != self._revision:
                return
            self._radar_snapshot = {"http_status": status, "result": copy.deepcopy(reading)}
            self._radar_sampled_at = self._clock()
            self._state["radar_timestamp"] = reading.get("timestamp")
            error = None
            distance = None
            age = reading.get("age_seconds", 0)
            stale_after = reading.get("stale_after_seconds", 2)
            if (status != 200 or reading.get("fresh") is not True
                    or not finite(age) or age < 0 or not finite(stale_after)
                    or not 0 < stale_after <= 2 or age > stale_after
                    or reading.get("serial_connected") is False):
                error = "Radar unavailable or stale; requesting lights off."
            elif self._state["mode"] == "distance_test" and reading.get("presence") != "none":
                distance = reading.get("range_estimate_cm")
                if type(distance) not in (int, float) or not math.isfinite(distance) or distance <= 0:
                    distance = None
                    error = "Radar target has no usable distance; requesting lights off."
            if self._state["mode"] == "auto":
                raw_presence = reading.get("presence")
                if not error and raw_presence not in ("none", "moving", "stationary", "both"):
                    error = "Invalid radar presence state; requesting lights off."
                distance_m = nearest_distance(reading, self.adaptive_config.max_distance_m)
                if not error and raw_presence != "none":
                    keys = ("moving_distance_cm",) if raw_presence == "moving" else ("stationary_distance_cm",) if raw_presence == "stationary" else ("moving_distance_cm", "stationary_distance_cm")
                    if not any(finite(reading.get(k)) and reading[k] >= 0 for k in keys):
                        error = "Radar target has no valid channel distance; requesting lights off."
                self._state.update(radar_presence_confirmed=reading.get("presence_confirmed"),
                                   radar_presence_state=reading.get("presence_state"),
                                   radar_nearest_distance_m=distance_m)
                if reading.get("range_filter_state") in ("weak_signal", "no_target"):
                    distance_m = None
                    gate_reason = "no_credible_range"
                elif "presence_confirmed" in reading and reading["presence_confirmed"] is not True:
                    distance_m = None
                    gate_reason = "unconfirmed_target"
                else:
                    gate_reason = "no_target" if raw_presence == "none" else "outside_detection_area" if distance_m is None else "valid_target"
                if error or not self._auto_allowed():
                    self._motion_session_active = False
                elif distance_m is not None and self.adaptive_config.require_motion_to_activate:
                    if (self.adaptive.lost_at is not None and self._clock() - self.adaptive.lost_at
                            >= self.adaptive_config.presence_timeout_seconds):
                        self._motion_session_active = False
                    moving_distance = reading.get("moving_distance_cm")
                    motion_in_area = (raw_presence in ("moving", "both")
                                      and finite(moving_distance)
                                      and 0 <= moving_distance <= self.adaptive_config.max_distance_m * 100
                                      and ("presence_state" not in reading
                                           or reading["presence_state"] == "PRESENT_ACTIVE"))
                    if motion_in_area:
                        self._motion_session_active = True
                    if not self._motion_session_active:
                        distance_m = None
                        gate_reason = "waiting_for_movement"
                self._state["presence_gate_reason"] = "sensor_unavailable" if error else gate_reason
                distance = None if distance_m is None else distance_m * 100
            led_count = getattr(self.transport, "led_count", None)
            if not isinstance(led_count, int) or led_count <= 0:
                led_count = 144
            max_index = led_count - 1
            if distance is None:
                index = None
            elif distance <= 100:
                index = 0
            elif distance >= 600:
                index = max_index
            else:
                # Reserve the end LEDs for their distance thresholds, even
                # when rounding a nearby distance would reach an endpoint.
                index = max(1, min(max_index - 1, round((distance - 100) / 500 * max_index)))
            self._state.update(distance_cm=distance, led_index=index if self._state["mode"] == "distance_test" else None,
                               test_state="sensor_unavailable" if error else "no_target" if index is None else "tracking")
            if error:
                self._state["sensor_error"] = error
            else:
                self._state.pop("sensor_error", None)
            if self._state["mode"] == "auto":
                self._evaluate_adaptive(error, distance)
            else:
                self._send(64 if index is not None else 0, color="#ffa028", led_index=index, instant=True)

    def _radar_age(self):
        if self._radar_snapshot is None or self._radar_sampled_at is None:
            return float("inf")
        age = self._radar_snapshot["result"].get("age_seconds", 0)
        return (age + max(0, self._clock() - self._radar_sampled_at)
                if finite(age) and age >= 0 else float("inf"))

    def update_migration(self, snapshot):
        with self._lock:
            self._migration_snapshot = copy.deepcopy(snapshot)
            if self._state["mode"] == "auto" and self._radar_snapshot is not None:
                self._evaluate_adaptive(self._state.get("sensor_error"), self._state.get("distance_cm"))

    def _evaluate_adaptive(self, error, distance):
        level, age, migration_error = migration_from_snapshot(self._migration_snapshot, self.adaptive_config)
        error = error or ("Radar sample stale" if self._radar_age() > 2 else None)
        previous_cap = self._state.get("migration_cap_percent", self.adaptive_config.unknown_migration_cap)
        sound_blocked = self.sound_reader is not None and self._sound.get("source") not in ("human", "unavailable")
        self._state["sound_gate"] = copy.deepcopy(self._sound) if self.sound_reader else None
        result = self.adaptive.step(self._clock(), self._auto_allowed() and not sound_blocked, not error,
                                    None if distance is None else distance / 100, level)
        if sound_blocked:
            result["lighting_state"] = "sound_blocked"
        state_key = (result["lighting_state"], level, bool(error))
        if state_key != self._last_adaptive_state:
            logging.getLogger(__name__).info("Adaptive lighting state=%s migration=%s cap=%s target=%s sensor_error=%s",
                result["lighting_state"], level, result["migration_cap_percent"], result["final_target_percent"], error)
            self._last_adaptive_state = state_key
        if result["lighting_state"] in ("ambient_off", "sensor_unavailable", "fading_off", "off"):
            self._motion_session_active = False
        self._state.update(result, migration_age_seconds=age, migration_error=migration_error,
                           motion_session_active=self._motion_session_active)
        self._auto_target = min(int(self.adaptive.output * 255 / 100),
                                int(result["migration_cap_percent"] * 255 / 100))
        self._send(self._auto_target, instant=self._auto_target == 0 or bool(error) or not self._auto_allowed()
                   or result["migration_cap_percent"] < previous_cap)

    def stop(self):
        with self._lock:
            if self._stopped:
                return
        self._radar_stop.set()
        if self._radar_worker:
            self._radar_worker.join(timeout=1)
        with self._condition:
            test_active = self._state["mode"] in ("auto", "distance_test")
            self._stopped = True
            self._pending = None
            self._condition.notify_all()
        if self._worker:
            self._worker.join(timeout=12)
        elif test_active:
            self._apply_result(0, self._request(0, instant=True))
        if self.transport and (self._worker is None or not self._worker.is_alive()):
            self.transport.close()

    def _work(self):
        last_attempt = None
        retry = False
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or self._pending is not None, timeout=0.5)
                if self._stopped:
                    test_active = self._state["mode"] in ("auto", "distance_test")
                    break
                if (self._state["mode"] == "auto" and not self._auto_allowed()
                        and self._state["target_brightness"]):
                    # Lux freshness has its own watchdog if collection stalls.
                    self._send(0, instant=True)
                radar_active = self._state["mode"] in ("auto", "distance_test")
                if (radar_active and self._radar_snapshot is not None
                        and self._radar_sampled_at is not None
                        and self._radar_age() > 2
                        and self._state["target_brightness"]):
                    # Independent watchdog: the output worker fails dark even
                    # if the sensor reader hangs or its sampling thread stops.
                    self.adaptive.reset()
                    self._motion_session_active = False
                    self._auto_target = 0
                    self._state.update(motion_session_active=False, output_percent=0, final_target_percent=0, lighting_state="sensor_unavailable", person_distance_m=None, presence_detected=False, distance_cm=None, led_index=None,
                                       test_state="sensor_unavailable",
                                       sensor_error="Radar sampling stopped; requesting lights off.")
                    self._send(0, instant=True)
                if self._pending is None:
                    interval = 0.5 if radar_active and retry else 2
                    # Manual on/off commands also recover after a lost USB or
                    # network reply. Only replay the newest desired state.
                    if (((not radar_active or self._state["mode"] == "auto") and not retry)
                            or self._desired is None
                            or (last_attempt is not None and self._clock() - last_attempt < interval)):
                        continue
                    self._pending = self._desired
                    self._state["status"] = "pending"
                command, self._pending = self._pending, None
                target, color, led_index, instant, revision, sample = command[:6]
                grp = command[6] if len(command) > 6 else 1
                spc = command[7] if len(command) > 7 else 0
                of = command[8] if len(command) > 8 else 0
            if grp == 1 and spc == 0 and of == 0:
                error = self._request(target, color, led_index, instant)
            else:
                try:
                    error = self._request(target, color, led_index, instant, grp=grp, spc=spc, of=of)
                except TypeError:
                    try:
                        error = self._request(target, color, led_index, instant, grp=grp, spc=spc)
                    except TypeError:
                        error = self._request(target, color, led_index, instant)
            with self._lock:
                last_attempt = self._clock()
                retry = error is not None
                if revision == self._revision:
                    self._apply_result(target, error, sample)
                self._condition.notify_all()
        if test_active:
            error = self._request(0, instant=True)
            with self._lock:
                self._apply_result(0, error)
        if self.transport:
            self.transport.close()

    def _send(self, target, color=None, led_index=None, instant=False, grp=1, spc=0, of=0):
        if self._state["mode"] == "auto" and not self._auto_allowed():
            # Gate all Auto paths, including appearance changes and watchdogs.
            if self._state["target_brightness"]:
                self._revision += 1
                self._pending = None
            target, instant = 0, True
            self.adaptive.reset()
            self._motion_session_active = False
            self._state.update(motion_session_active=False, final_target_percent=0, output_percent=0, lighting_state="ambient_off")
            self._auto_target = 0
            self._auto_last_distance = self._auto_last_change = None
        if self._state["mode"] == "auto":
            cap = self._state.get("migration_cap_percent", self.adaptive_config.unknown_migration_cap)
            target = min(target, int(cap * 255 / 100))
        if (self._state["mode"] == "auto" and self._state["target_brightness"] is not None
                and target < self._state["target_brightness"]):
            self._revision += 1
        color = color or self._state["color"]
        signature = (target, color, led_index, grp, spc, of)
        now = self._clock()
        sample = {"confirmed_led_index": led_index if target else None,
                  "confirmed_distance_cm": self._state.get("distance_cm") if target else None,
                  "confirmed_radar_timestamp": self._state.get("radar_timestamp")}
        self._desired = (target, color, led_index, instant, self._revision, sample, grp, spc, of)
        if (self._state["mode"] in ("auto", "distance_test")
                and signature == self._last_distance_command
                and self._state["status"] != "controller_unavailable"
                and self._last_distance_sent_at is not None
                and now - self._last_distance_sent_at < (1 if self._state["mode"] == "auto" else 2)):
            return
        if self._state["mode"] in ("auto", "distance_test"):
            self._last_distance_command, self._last_distance_sent_at = signature, now
        self._state["target_brightness"] = target
        self._state["status"] = "pending"
        if self._background:
            self._pending = self._desired
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, daemon=True, name="lighting-control")
                self._worker.start()
            self._condition.notify()
        else:
            if grp == 1 and spc == 0 and of == 0:
                res = self._request(target, color, led_index, instant)
            else:
                try:
                    res = self._request(target, color, led_index, instant, grp=grp, spc=spc, of=of)
                except TypeError:
                    try:
                        res = self._request(target, color, led_index, instant, grp=grp, spc=spc)
                    except TypeError:
                        res = self._request(target, color, led_index, instant)
            self._apply_result(target, res, sample)

    def _request(self, target, color="#ffd6aa", led_index=None, instant=False, grp=1, spc=0, of=0):
        # Periodic reassertion restores output after fixture restarts or other
        # streams; unchanged distance commands are coalesced in _send.
        params = {"on": bool(target), "bri": target, "transition": 1 if self._state["mode"] == "auto" else 3,
                  "lor": 2, "live": False, "udpn": {"nn": True},
                  "seg": [{"id": 0, "on": True, "bri": 255, "fx": 0, "pal": 0,
                           "grp": grp, "spc": spc, "of": of,
                           "col": [[int(color[index:index + 2], 16) for index in (1, 3, 5)] + [0]]}]}
        if instant:
            params["transition"] = 0
        if led_index is not None:
            params["seg"][0].update(start=led_index, stop=led_index + 1)
        params["bioglow"] = {"watchdog_ms": 3000 if self._state["mode"] == "auto" else 0}
        try:
            if self.transport:
                device = self.transport.send(params)
                if isinstance(device, dict):
                    self._device_info = device
                return None
            host = self.host
            if self._background:
                if self._address is None:
                    try:
                        self._address = socket.gethostbyname(host)
                    except socket.gaierror:
                        if not host.endswith(".local"):
                            raise
                        # Use the installed system bus client, without requiring
                        # the optional avahi-utils command-line package.
                        try:
                            resolved = subprocess.run(
                                ["busctl", "--system", "--timeout=2", "--json=short", "--", "call",
                                 "org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server",
                                 "ResolveHostName", "iisiu", "-1", "-1", host, "0", "0"],
                                capture_output=True, text=True, timeout=2.5, check=True)
                            reply = json.loads(resolved.stdout)
                            address = reply["data"][4]
                            self._address = str(ipaddress.IPv4Address(address))
                        except (OSError, subprocess.SubprocessError, ValueError,
                                KeyError, IndexError, TypeError) as exc:
                            raise OSError(
                                f"Cannot resolve {host} through DNS or system Avahi. "
                                "Check the fixture connection and Avahi service, or set "
                                "BIOGLOW_LIGHT_HOST to its LAN IP address"
                            ) from exc
                host = self._address
            request = Request(f"http://{host}/json/state",
                          data=json.dumps(params).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
            with self._opener(request, timeout=3) as response:
                if response.status != 200:
                    raise OSError(f"Controller returned HTTP {response.status}")
            return None
        except Exception as exc:
            self._address = None
            device_info = getattr(self.transport, "device_info", None)
            if isinstance(device_info, dict):
                self._device_info = dict(device_info)
            return f"WLED controller ({'USB' if self.transport else self.host}) unavailable: {str(exc).rstrip('.')}. Lux measurements continue."

    def _apply_result(self, target, error, sample=None):
        if error is None:
            self._last_ack_at = self._clock()
            self._state["brightness"] = target
            self._state["on"] = bool(target)
            if target == 0 and self._state["mode"] in ("auto", "distance_test"):
                self._state.update(confirmed_led_index=None, confirmed_distance_cm=None)
            if sample is not None and self._state["mode"] in ("auto", "distance_test"):
                self._state.update(sample)
        if error is not None:
            self._state.update(status="controller_unavailable", error=error)
            return
        if self._pending is not None or self._state["status"] in ("waiting", "sensor_unavailable"):
            return
        if error is None:
            self._state.update(status="ok")
            self._state.pop("error", None)
