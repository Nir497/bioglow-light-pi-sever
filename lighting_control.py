"""Server-owned lux control for the BioGlow WLED fixture."""
import json
import math
import os
import socket
import subprocess
import threading
import time
from collections import deque
from urllib.request import Request, urlopen, build_opener, ProxyHandler


class LightingControl:
    def __init__(self, host="bioglow.local", opener=urlopen, clock=time.monotonic, background=False):
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
        self._samples = deque()
        self._on = False
        self._auto_target = None
        self._state = {"name": "BioGlow", "hostname": self.host, "brightness": None,
                       "target_brightness": None, "status": "waiting", "mode": "auto",
                       "on_below_lux": 30, "off_above_lux": 300}

    def status(self):
        with self._lock:
            return dict(self._state)

    def set_mode(self, mode):
        if mode not in ("on", "off", "auto"):
            raise ValueError('mode must be "on", "off", or "auto"')
        with self._lock:
            self._state["mode"] = mode
            target = self._auto_target if mode == "auto" else (255 if mode == "on" else 0)
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
                    self._send(255 if self._state["mode"] == "on" else 0)
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
                target = 255 if self._state["mode"] == "on" else 0
            self._send(target)

    def stop(self):
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        if self._worker:
            self._worker.join(timeout=6)

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or self._pending is not None)
                if self._stopped:
                    return
                target, self._pending = self._pending, None
            error = self._request(target)
            with self._lock:
                self._apply_result(target, error)

    def _send(self, target):
        self._state["target_brightness"] = target
        if self._background:
            self._pending = target
            self._state["status"] = "pending"
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, daemon=True, name="lighting-control")
                self._worker.start()
            self._condition.notify()
        else:
            self._state["status"] = "pending"
            self._apply_result(target, self._request(target))

    def _request(self, target):
        # Reassert each sample so a fixture restart or another stream cannot
        # silently leave the physical output out of sync with the server.
        params = {"on": bool(target), "bri": target, "transition": 30,
                  "lor": 2, "live": False, "udpn": {"nn": True},
                  "seg": [{"id": 0, "on": True, "bri": 255, "fx": 0,
                           "col": [[255, 160, 40, 255]]}]}
        try:
            host = self.host
            if self._background:
                if self._address is None:
                    try:
                        self._address = socket.gethostbyname(host)
                    except socket.gaierror:
                        # Avahi can resolve LAN mDNS even without an NSS mDNS module.
                        resolved = subprocess.run(["avahi-resolve-host-name", "-4", host],
                                                  capture_output=True, text=True, timeout=2, check=True)
                        self._address = resolved.stdout.split()[1]
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
            return f"BioGlow lighting controller ({self.host}) unavailable: {exc}. Lux measurements continue."

    def _apply_result(self, target, error):
        if error is None:
            self._state["brightness"] = target
        if self._pending is not None or self._state["status"] in ("waiting", "sensor_unavailable"):
            return
        if error is None:
            self._state.update(status="ok")
            self._state.pop("error", None)
        else:
            self._state.update(status="controller_unavailable", error=error)
