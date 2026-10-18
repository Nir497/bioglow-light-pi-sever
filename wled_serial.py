"""WLED JSON commands over USB serial; no network or second server required."""
import copy
import json
import os
import time

import serial
from serial.tools import list_ports


class WLEDSerial:
    def __init__(self, port=None, baudrate=115200, led_count=144):
        self.port = port or os.environ.get("BIOGLOW_WLED_PORT")
        self.baudrate = baudrate
        self.connection = None
        self.led_count = led_count
        self.device_info = {"model": "SP803E", "expected_led_count": led_count, "length_ft": 3.2}

    def _read_reply(self):
        deadline = time.monotonic() + 3
        pending = b""
        while time.monotonic() < deadline:
            pending += self.connection.read_until(b"\n", 32768)
            if len(pending) > 65536:
                raise OSError("WLED USB response exceeds the supported size")
            try:
                reply = json.loads(pending)
            except (ValueError, UnicodeDecodeError):
                if pending.endswith(b"\n"):
                    pending = b""  # Ignore complete boot/debug lines.
                continue
            pending = b""
            if isinstance(reply, dict) and isinstance(reply.get("state"), dict):
                return reply
        raise OSError("No WLED JSON response over USB. Check WLED serial support and 115200 baud.")

    def _write(self, params):
        data = (json.dumps(params, separators=(",", ":")) + "\n").encode("ascii")
        if self.connection.write(data) != len(data):
            raise OSError("Incomplete WLED USB command")

    def send(self, params):
        try:
            current = None
            if self.connection is None:
                port = self.port
                if not port:
                    ports = [item.device for item in list_ports.comports() if item.vid is not None]
                    if len(ports) != 1:
                        raise OSError("Connect the WLED USB controller to the server Pi. "
                                      "If multiple USB serial devices are present, set BIOGLOW_WLED_PORT "
                                      "to its /dev/serial/by-id path.")
                    port = ports[0]
                connection = serial.Serial(port=None, baudrate=self.baudrate,
                                           timeout=0.25, write_timeout=1, exclusive=True)
                self.connection = connection
                # Avoid asserting the ESP boot/reset lines when opening USB.
                connection.dtr = False
                connection.rts = False
                connection.port = port
                connection.open()
                self._write({"v": True})
                reply = self._read_reply()
                if reply.get("info", {}).get("brand") != "WLED":
                    raise OSError("The selected USB serial device did not identify as WLED")
                current = reply
            # Query before each command: LED count and segment layout may have
            # changed in WLED since the preceding command.
            if current is None:
                self.connection.reset_input_buffer()
                self._write({"v": True})
                current = self._read_reply()
            info = current.get("info", {})
            count = info.get("leds", {}).get("count")
            if type(count) is not int or not 1 <= count <= 65535:
                raise OSError("WLED did not report a valid configured LED count; check LED Preferences")
            if not str(info.get("arch", "esp32")).lower().startswith("esp32"):
                raise OSError("Expected the SP803E ESP32 WLED controller")
            self.device_info = {"model": "SP803E", "name": info.get("name", "WLED-ESP32"),
                                "firmware": info.get("ver"), "architecture": info.get("arch", "esp32"),
                                "led_count": count, "expected_led_count": self.led_count, "length_ft": 3.2}
            if count < self.led_count and params["on"]:
                raise OSError(f"WLED is configured for {count} pixels, but your strip has {self.led_count}. "
                              f"Set the strip output length to {self.led_count} in WLED LED Preferences. "
                              "WLED's USB state API cannot change the LED-output hardware configuration.")
            coverage = min(count, self.led_count)
            command = copy.deepcopy(params)
            full = command["seg"][0]
            full.update(id=0, start=0, stop=coverage, grp=1, spc=0, of=0,
                        on=True, bri=255, fx=0, frz=False, rev=False, mi=False)
            # Consolidate the entire strip into segment 0 so overlapping old
            # spotlight/music segments cannot override part of the output.
            command["seg"] = [full] + [
                {"id": item["id"], "stop": 0} for item in current["state"].get("seg", [])
                if type(item.get("id")) is int and item["id"] != 0]
            command.update(mainseg=0, pl=-1, nl={"on": False}, v=True)
            self._write(command)
            acknowledged = self._read_reply()
            state = acknowledged["state"]
            if state.get("on") != params["on"] or (params["on"] and state.get("bri") != params["bri"]):
                raise OSError("WLED did not acknowledge the requested power and brightness")
            segments = state.get("seg", [])
            segment = next((item for item in segments if item.get("id", 0) == 0), None)
            if not segment or segment.get("col", [[]])[0][:3] != params["seg"][0]["col"][0][:3]:
                raise OSError("WLED did not acknowledge the requested color")
            if any(segment.get(key) != value for key, value in
                   {"start": 0, "stop": coverage, "grp": 1, "spc": 0,
                    "on": True, "bri": 255, "fx": 0, "frz": False, "of": 0}.items()):
                raise OSError("WLED did not acknowledge full-strip coverage with a solid color")
            if any(item.get("id", 0) != 0 and item.get("stop", 0) > item.get("start", 0)
                   for item in segments):
                raise OSError("WLED still reports other active segments over the strip")
            self.device_info.update(covered_leds=coverage, full_strip_confirmed=coverage == self.led_count)
            return dict(self.device_info)
        except Exception:
            self.close()
            raise

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None
