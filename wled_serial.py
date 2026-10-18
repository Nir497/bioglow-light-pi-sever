"""WLED JSON commands over USB serial; no network or second server required."""
import copy
import json
import os
import time

import serial
from serial.tools import list_ports


class SerialReplyTimeout(OSError):
    """A serial port opened, but no complete WLED state response arrived."""


class WLEDSerial:
    def __init__(self, port=None, baudrate=115200, led_count=192, clock=time.monotonic):
        self.port = port or os.environ.get("BIOGLOW_WLED_PORT")
        self.baudrate = baudrate
        self.connection = None
        self.led_count = led_count
        self._clock = clock
        self._current = None
        self._queried_at = None
        self.device_info = {"model": "SP803E", "expected_led_count": led_count, "length_ft": 3.2}

    def _read_reply(self):
        deadline = time.monotonic() + 3
        pending = b""
        decoder = json.JSONDecoder()
        while time.monotonic() < deadline:
            chunk = self.connection.read_until(b"\n", 32768)
            self.device_info["received_bytes"] = self.device_info.get("received_bytes", 0) + len(chunk)
            pending += chunk
            if len(pending) > 65536:
                raise OSError("WLED USB response exceeds the supported size")
            # ESP boot/debug output may precede JSON without a newline. Scan
            # for a complete state object rather than dropping the entire line.
            text = pending.decode("utf-8", errors="replace")
            start = text.find("{")
            while start >= 0:
                try:
                    reply, _ = decoder.raw_decode(text[start:])
                except ValueError:
                    start = text.find("{", start + 1)
                    continue
                if isinstance(reply, dict) and isinstance(reply.get("state"), dict):
                    self.device_info["response_kind"] = "WLED JSON"
                    return reply
                start = text.find("{", start + 1)
            if chunk:
                self.device_info["response_kind"] = "serial output without WLED JSON"
                if "esphome" in text.lower():
                    self.device_info["response_kind"] = "ESPHome output (not WLED firmware)"
        port = self.device_info.get("serial_port", self.port or "selected USB port")
        received = self.device_info.get("received_bytes", 0)
        kind = self.device_info.get("response_kind", "no serial data")
        raise SerialReplyTimeout(
            f"No WLED JSON response from {port} at {self.baudrate} baud "
            f"({received} bytes received; {kind}). Check the selected USB device, "
            "WLED serial baud rate, firmware serial support, and RX/TX pin assignments")

    def _query(self, startup=False):
        for attempt in range(2 if startup else 1):
            self._write({"v": True})
            try:
                return self._read_reply()
            except SerialReplyTimeout:
                if not startup or attempt == 1:
                    raise
                # Opening some USB bridges resets the ESP. Retry on the same
                # open port after the first response window; reopening can reset
                # it again and prevent it ever answering the initial query.

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
                self.device_info.update(serial_port=port, baudrate=self.baudrate,
                                        received_bytes=0, response_kind="no serial data")
                connection.open()
                reply = self._query(startup=True)
                if reply.get("info", {}).get("brand") != "WLED":
                    raise OSError("The selected USB serial device did not identify as WLED")
                current = reply
                self._queried_at = self._clock()
            # Reuse the last acknowledged state during rapid updates. Refresh
            # configuration every five seconds and after every reconnect/error.
            if current is None:
                if self._current is not None and self._queried_at is not None and self._clock() - self._queried_at < 5:
                    current = self._current
                else:
                    self.connection.reset_input_buffer()
                    current = self._query()
                    self._queried_at = self._clock()
            info = current.get("info", {})
            count = info.get("leds", {}).get("count")
            if type(count) is not int or not 1 <= count <= 65535:
                raise OSError("WLED did not report a valid configured LED count; check LED Preferences")
            if not str(info.get("arch", "esp32")).lower().startswith("esp32"):
                raise OSError("Expected the SP803E ESP32 WLED controller")
            self.device_info = {"serial_port": self.device_info.get("serial_port"),
                                "baudrate": self.baudrate,
                                "model": "SP803E", "name": info.get("name", "WLED-ESP32"),
                                "firmware": info.get("ver"), "architecture": info.get("arch", "esp32"),
                                "led_count": count, "expected_led_count": self.led_count, "length_ft": 3.2}
            if count < self.led_count and params["on"]:
                raise OSError(f"WLED is configured for {count} pixels, but your strip has {self.led_count}. "
                              f"Set the strip output length to {self.led_count} in WLED LED Preferences. "
                              "WLED's USB state API cannot change the LED-output hardware configuration.")
            coverage = min(count, self.led_count)
            command = copy.deepcopy(params)
            full = command["seg"][0]
            start, stop = full.get("start", 0), full.get("stop", coverage)
            if type(start) is not int or type(stop) is not int or not 0 <= start < stop <= coverage:
                raise OSError("Requested LED range is outside the configured strip")
            full.update(id=0, start=start, stop=stop, grp=1, spc=0, of=0,
                        on=True, bri=255, fx=0, pal=0, frz=False, rev=False, mi=False)
            # Consolidate the entire strip into segment 0 so overlapping old
            # spotlight/music segments cannot override part of the output.
            command["seg"] = [full] + [
                {"id": item["id"], "stop": 0} for item in current["state"].get("seg", [])
                if type(item.get("id")) is int and item["id"] != 0]
            command.update(mainseg=0, pl=-1, nl={"on": False}, v=True)
            self._write(command)
            acknowledged = self._read_reply()
            reported_count = acknowledged.get("info", {}).get("leds", {}).get("count", count)
            if reported_count != count:
                raise OSError("WLED LED count changed during the command; refreshing configuration")
            state = acknowledged["state"]
            if state.get("on") != params["on"] or (params["on"] and state.get("bri") != params["bri"]):
                raise OSError("WLED did not acknowledge the requested power and brightness")
            segments = state.get("seg", [])
            segment = next((item for item in segments if item.get("id", 0) == 0), None)
            requested_color = (list(params["seg"][0]["col"][0]) + [0])[:4]
            reported_color = (list(segment.get("col", [[]])[0]) + [0])[:4] if segment else []
            if reported_color != requested_color:
                raise OSError("WLED did not acknowledge the requested RGBW color")
            if any(segment.get(key) != value for key, value in
                   {"start": start, "stop": stop, "grp": 1, "spc": 0,
                    "on": True, "bri": 255, "fx": 0, "pal": 0, "frz": False, "of": 0}.items()):
                raise OSError("WLED did not acknowledge full-strip coverage or requested LED range with a solid color")
            if any(item.get("id", 0) != 0 and item.get("stop", 0) > item.get("start", 0)
                   for item in segments):
                raise OSError("WLED still reports other active segments over the strip")
            power = acknowledged.get("info", {}).get("leds", {})
            self.device_info.update(
                covered_leds=stop - start, full_strip_confirmed=start == 0 and stop == self.led_count,
                confirmed_led_index=start if stop - start == 1 else None,
                confirmed_color="#" + "".join(f"{value:02x}" for value in reported_color[:3]),
                confirmed_white=reported_color[3], confirmed_brightness=state.get("bri"),
                confirmed_on=state.get("on"), segment_brightness=segment["bri"],
                current_limit_ma=power.get("maxpwr"), estimated_current_ma=power.get("pwr"))
            ack_info = acknowledged.get("info", {})
            cached_info = {**info, **ack_info}
            cached_info["leds"] = {**info.get("leds", {}), **ack_info.get("leds", {})}
            self._current = {"state": copy.deepcopy(state), "info": cached_info}
            return dict(self.device_info)
        except Exception:
            self.close()
            raise

    def close(self):
        self._current = None
        self._queried_at = None
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None
