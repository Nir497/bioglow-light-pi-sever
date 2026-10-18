"""Server-owned lux control for the BioGlow WLED fixture."""
import json
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
            return {**self._state, "device": dict(self._device_info)}

    def set_mode(self, mode):
        return self.configure(mode=mode)

    def configure(self, mode=None, brightness=None, color=None):
        if mode is not None and mode not in ("on", "off", "auto"):
            raise ValueError('mode must be "on", "off", or "auto"')
        if brightness is not None and (type(brightness) is not int or not 1 <= brightness <= 255):
            raise ValueError("brightness must be an integer from 1 to 255")
        if color is not None and (not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)):
            raise ValueError("color must be a hex RGB value such as #ff8000")
        if mode is None and brightness is None and color is None:
            raise ValueError("Provide mode, brightness, or color")
        with self._lock:
            if brightness is not None:
                self._state["manual_brightness"] = brightness
            if color is not None:
                self._state["color"] = color.lower()
            if mode is None:
                mode = "off" if self._state["mode"] == "off" else "on"
            self._state["mode"] = mode
            target = self._auto_target if mode == "auto" else (self._state["manual_brightness"] if mode == "on" else 0)
            if target is None:
                self._state.update(status="waiting", target_brightness=None)
                self._state.pop("error", None)
            else:
                self._send(target)
            return dict(self._state)

    def update(self, lux):
        with self._lock:
            now = self._clock()
            while self._samples and now - self._samples[0][0] >= 30:
                self._samples.popleft()
            if lux is None or not isinstance(lux, (int, float)) or not math.isfinite(lux) or lux < 0:
                self._auto_target = None
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
            if self._state["mode"] != "auto":
                target = self._state["manual_brightness"] if self._state["mode"] == "on" else 0
            self._send(target)

    def stop(self):
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        if self._worker:
            self._worker.join(timeout=6)
        if self.transport and (self._worker is None or not self._worker.is_alive()):
            self.transport.close()

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or self._pending is not None)
                if self._stopped:
                    if self.transport:
                        self.transport.close()
                    return
                command, self._pending = self._pending, None
                target, color = command
            error = self._request(target, color)
            with self._lock:
                self._apply_result(target, error)

    def _send(self, target):
        self._state["target_brightness"] = target
        if self._background:
            self._pending = (target, self._state["color"])
            self._state["status"] = "pending"
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, daemon=True, name="lighting-control")
                self._worker.start()
            self._condition.notify()
        else:
            self._state["status"] = "pending"
            self._apply_result(target, self._request(target, self._state["color"]))

    def _request(self, target, color="#ffa028"):
        # Reassert each sample so a fixture restart or another stream cannot
        # silently leave the physical output out of sync with the server.
        params = {"on": bool(target), "bri": target, "transition": 30,
                  "lor": 2, "live": False, "udpn": {"nn": True},
                  "seg": [{"id": 0, "on": True, "bri": 255, "fx": 0, "pal": 0,
                           "col": [[int(color[index:index + 2], 16) for index in (1, 3, 5)] + [0]]}]}
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

    def _apply_result(self, target, error):
        if error is None:
            self._state["brightness"] = target
            self._state["on"] = bool(target)
        if self._pending is not None or self._state["status"] in ("waiting", "sensor_unavailable"):
            return
        if error is None:
            self._state.update(status="ok")
            self._state.pop("error", None)
        else:
            self._state.update(status="controller_unavailable", error=error)
