"""Server-owned lux control for the BioGlow WLED fixture."""
import json
import math
import threading
import time
from collections import deque
from urllib.request import Request, urlopen


class LightingControl:
    def __init__(self, host="bioglow.local", opener=urlopen, clock=time.monotonic):
        self.host = host
        self._opener = opener
        self._clock = clock
        self._lock = threading.Lock()
        self._samples = deque()
        self._on = False
        self._applied = None
        self._state = {"name": "BioGlow", "hostname": host, "brightness": None,
                       "target_brightness": None, "status": "waiting",
                       "on_below_lux": 30, "off_above_lux": 300}

    def status(self):
        with self._lock:
            return dict(self._state)

    def update(self, lux):
        with self._lock:
            now = self._clock()
            while self._samples and now - self._samples[0][0] >= 30:
                self._samples.popleft()
            if lux is None or not isinstance(lux, (int, float)) or not math.isfinite(lux) or lux < 0:
                self._state.update(status="sensor_unavailable", error="Lux unavailable; retaining last brightness.")
                return
            self._samples.append((now, lux))
            average = sum(value for _, value in self._samples) / len(self._samples)
            if average < 30:
                self._on = True
            elif average > 300:
                self._on = False
            target = round(50 + 205 * max(0, 1 - average / 30)) if self._on else 0
            self._state.update(smoothed_lux=average, target_brightness=target)
            # Reassert each sample so a fixture restart or another stream cannot
            # silently leave the physical output out of sync with the server.
            params = {"on": bool(target), "bri": target, "transition": 30,
                      "lor": 2, "live": False, "udpn": {"nn": True},
                      "seg": [{"id": 0, "on": True, "bri": 255, "fx": 0,
                               "col": [[255, 160, 40, 255]]}]}
            request = Request(f"http://{self.host}/json/state",
                              data=json.dumps(params).encode("utf-8"),
                              headers={"Content-Type": "application/json"})
            try:
                with self._opener(request, timeout=3) as response:
                    if response.status != 200:
                        raise OSError(f"Controller returned HTTP {response.status}")
                self._applied = target
                self._state.update(status="ok", brightness=target)
                self._state.pop("error", None)
            except Exception as exc:
                self._state.update(status="controller_unavailable", error=str(exc))
