"""Server-owned lux and development distance control for the BioGlow fixture."""
import json
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
from urllib.request import Request, urlopen, build_opener, ProxyHandler


class LightingControl:
    def __init__(self, host="bioglow.local", opener=urlopen, clock=time.monotonic, background=False, transport=None):
        self.transport = transport
        self.host = os.environ.get("BIOGLOW_LIGHT_HOST", host)
        self._opener = build_opener(ProxyHandler({})).open if opener is urlopen else opener
        self._clock = clock
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._pending = None
        self._stopped = False
        self._background = background
        self._worker = None
        self._radar_worker = None
        self._radar_stop = threading.Event()
        self._presence_reader = None
        self._revision = 0
        self._radar_snapshot = None
        self._radar_sampled_at = None
        self._last_distance_command = None
        self._last_distance_sent_at = None
        self._address = None
        device_info = getattr(transport, "device_info", None)
        self._device_info = dict(device_info) if isinstance(device_info, dict) else {}
        self._samples = deque()
        self._on = False
        self._auto_target = None
        self._state = {"name": "BioGlow", "hostname": self.host, "brightness": None,
                       "target_brightness": None, "status": "waiting", "mode": "auto",
                       "connection": "usb" if transport else "wifi",
                       "manual_brightness": 255, "color": "#ffa028", "on": None,
                       "on_below_lux": 30, "off_above_lux": 300}

    def status(self):
        with self._lock:
            result = {**self._state, "device": dict(self._device_info)}
            if self._state["mode"] == "distance_test" and self._radar_snapshot is not None:
                radar = copy.deepcopy(self._radar_snapshot)
                age = (radar["result"].get("age_seconds") or 0) + max(0, self._clock() - self._radar_sampled_at)
                radar["result"]["age_seconds"] = age
                if age > radar["result"].get("stale_after_seconds", 2):
                    radar = {"http_status": 503, "result": {
                        "fresh": False, "status": "unavailable", "sensor_healthy": False,
                        "timestamp": radar["result"].get("timestamp"), "age_seconds": age,
                        "error": {"code": "presence_unavailable", "message": "Sampled radar frame is stale."}}}
                    result.update(distance_cm=None, led_index=None, test_state="sensor_unavailable",
                                  sensor_error="Sampled radar frame is stale; awaiting fresh sensor data.")
                result["radar_snapshot"] = radar
            return result

    def set_mode(self, mode):
        return self.configure(mode=mode)

    def configure(self, mode=None, brightness=None, color=None):
        if mode is not None and mode not in ("on", "off", "auto", "distance_test"):
            raise ValueError('mode must be "on", "off", "auto", or "distance_test"')
        if brightness is not None and (type(brightness) is not int or not 1 <= brightness <= 255):
            raise ValueError("brightness must be an integer from 1 to 255")
        if color is not None and (not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
            raise ValueError("color must be a hex RGB value such as #ff8000")
        if mode is None and brightness is None and color is None:
            raise ValueError("Provide mode, brightness, or color")
        with self._lock:
            if self._stopped:
                raise ValueError("Lighting control has stopped")
            if self._state["mode"] == "distance_test" and mode is None:
                raise ValueError("Select On, Off, or Auto before changing appearance")
            previous_mode = self._state["mode"]
            if brightness is not None:
                self._state["manual_brightness"] = brightness
            if color is not None:
                self._state["color"] = color.lower()
            if mode is None:
                mode = "off" if self._state["mode"] == "off" else "on"
            self._state["mode"] = mode
            self._revision += 1
            self._pending = None
            self._radar_snapshot = None
            self._last_distance_command = None
            self._state.pop("sensor_error", None)
            self._state.pop("error", None)
            if mode == "distance_test":
                self._state.update(distance_cm=None, led_index=None, sample_interval_seconds=0.5,
                                   test_state="waiting", test_brightness=64, test_color="#ffa028",
                                   distance_source="range_estimate_cm", radar_timestamp=None,
                                   confirmed_led_index=None, confirmed_distance_cm=None, confirmed_radar_timestamp=None)
                self._send(0, color="#ffa028", instant=True)
                return dict(self._state)
            for key in ("distance_cm", "led_index", "test_state", "test_brightness", "test_color", "sample_interval_seconds", "distance_source", "radar_timestamp", "confirmed_led_index",
                        "confirmed_distance_cm", "confirmed_radar_timestamp"):
                self._state.pop(key, None)
            target = self._auto_target if mode == "auto" else (self._state["manual_brightness"] if mode == "on" else 0)
            if target is None and previous_mode == "distance_test":
                self._send(0)
            if target is None:
                self._state.update(status="waiting", target_brightness=None)
                self._state.pop("error", None)
            else:
                self._send(target)
            return dict(self._state)

    def update(self, lux):
        with self._lock:
            if self._stopped:
                return
            distance_test = self._state["mode"] == "distance_test"
            now = self._clock()
            while self._samples and now - self._samples[0][0] >= 30:
                self._samples.popleft()
            if lux is None or not isinstance(lux, (int, float)) or not math.isfinite(lux) or lux < 0:
                self._auto_target = None
                if distance_test:
                    return
                if self._state["mode"] != "auto":
                    self._send(self._state["manual_brightness"] if self._state["mode"] == "on" else 0)
                    return
                self._state.update(status="sensor_unavailable", error="Lux unavailable; retaining last brightness.")
                return
            self._samples.append((now, lux))
            average = sum(value for _, value in self._samples) / len(self._samples)
            if average < 30:
                self._on = True
            elif average > 300:
                self._on = False
            target = round(50 + 205 * max(0, 1 - average / 30)) if self._on else 0
            self._auto_target = target
            self._state.update(smoothed_lux=average)
            if distance_test:
                return
            if self._state["mode"] != "auto":
                target = self._state["manual_brightness"] if self._state["mode"] == "on" else 0
            self._send(target)

    def start_distance_sampling(self, presence_reader):
        """Sample the existing UART receiver; never open a second sensor connection."""
        with self._lock:
            if self._radar_worker is not None or self._stopped:
                return
            self._presence_reader = presence_reader
            self._radar_worker = threading.Thread(target=self._sample_radar, daemon=True,
                                                  name="distance-light-sampling")
            self._radar_worker.start()

    def _sample_radar(self):
        deadline = self._clock()
        while not self._radar_stop.is_set():
            with self._lock:
                active = self._state["mode"] == "distance_test"
                revision = self._revision
            if active:
                try:
                    status, reading = self._presence_reader()
                except Exception as exc:
                    status, reading = 503, {"error": str(exc)}
                self.update_distance(status, reading, revision)
            deadline += 0.5
            now = self._clock()
            if deadline <= now:
                deadline = now + 0.5
            self._radar_stop.wait(max(0, deadline - now))

    def update_distance(self, status, reading, revision=None):
        with self._lock:
            if self._stopped or self._state["mode"] != "distance_test":
                return
            if revision is not None and revision != self._revision:
                return
            self._radar_snapshot = {"http_status": status, "result": copy.deepcopy(reading)}
            self._radar_sampled_at = self._clock()
            self._state["radar_timestamp"] = reading.get("timestamp")
            error = None
            distance = None
            if status != 200 or not reading.get("fresh", False):
                error = "Radar unavailable or stale; requesting lights off."
            elif reading.get("presence") != "none":
                distance = reading.get("range_estimate_cm")
                if type(distance) not in (int, float) or not math.isfinite(distance) or distance <= 0:
                    distance = None
                    error = "Radar target has no usable distance; requesting lights off."
            if distance is None:
                index = None
            elif distance <= 100:
                index = 0
            elif distance >= 600:
                index = 191
            else:
                # Reserve the end LEDs for their distance thresholds, even
                # when rounding a nearby distance would reach an endpoint.
                index = max(1, min(190, round((distance - 100) / 500 * 191)))
            self._state.update(distance_cm=distance, led_index=index,
                               test_state="sensor_unavailable" if error else "no_target" if index is None else "tracking")
            if error:
                self._state["sensor_error"] = error
            else:
                self._state.pop("sensor_error", None)
            self._send(64 if index is not None else 0, color="#ffa028", led_index=index, instant=True)

    def stop(self):
        with self._lock:
            if self._stopped:
                return
        self._radar_stop.set()
        if self._radar_worker:
            self._radar_worker.join(timeout=1)
        with self._condition:
            test_active = self._state["mode"] == "distance_test"
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
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or self._pending is not None)
                if self._stopped:
                    test_active = self._state["mode"] == "distance_test"
                    break
                command, self._pending = self._pending, None
                target, color, led_index, instant, revision, sample = command
            error = self._request(target, color, led_index, instant)
            with self._lock:
                if revision == self._revision:
                    self._apply_result(target, error, sample)
                self._condition.notify_all()
        if test_active:
            error = self._request(0, instant=True)
            with self._lock:
                self._apply_result(0, error)
        if self.transport:
            self.transport.close()

    def _send(self, target, color=None, led_index=None, instant=False):
        color = color or self._state["color"]
        signature = (target, color, led_index)
        now = self._clock()
        if (instant and self._state["mode"] == "distance_test"
                and signature == self._last_distance_command
                and self._state["status"] != "controller_unavailable"
                and self._last_distance_sent_at is not None and now - self._last_distance_sent_at < 2):
            return
        if instant:
            self._last_distance_command, self._last_distance_sent_at = signature, now
        self._state["target_brightness"] = target
        sample = {"confirmed_led_index": led_index if target else None,
                  "confirmed_distance_cm": self._state.get("distance_cm") if target else None,
                  "confirmed_radar_timestamp": self._state.get("radar_timestamp")}
        self._state["status"] = "pending"
        if self._background:
            self._pending = (target, color, led_index, instant, self._revision, sample)
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, daemon=True, name="lighting-control")
                self._worker.start()
            self._condition.notify()
        else:
            self._apply_result(target, self._request(target, color, led_index, instant), sample)

    def _request(self, target, color="#ffa028", led_index=None, instant=False):
        # Periodic reassertion restores output after fixture restarts or other
        # streams; unchanged distance commands are coalesced in _send.
        params = {"on": bool(target), "bri": target, "transition": 30,
                  "lor": 2, "live": False, "udpn": {"nn": True},
                  "seg": [{"id": 0, "on": True, "bri": 255, "fx": 0, "pal": 0,
                           "col": [[int(color[index:index + 2], 16) for index in (1, 3, 5)] + [0]]}]}
        if instant:
            params["transition"] = 0
        if led_index is not None:
            params["seg"][0].update(start=led_index, stop=led_index + 1)
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
            self._state["brightness"] = target
            self._state["on"] = bool(target)
            if target == 0 and self._state["mode"] == "distance_test":
                self._state.update(confirmed_led_index=None, confirmed_distance_cm=None)
            if sample is not None and self._state["mode"] == "distance_test":
                self._state.update(sample)
        if error is not None:
            self._state.update(status="controller_unavailable", error=error)
            return
        if self._pending is not None or self._state["status"] in ("waiting", "sensor_unavailable"):
            return
        if error is None:
            self._state.update(status="ok")
            self._state.pop("error", None)
