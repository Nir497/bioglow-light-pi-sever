"""WLED JSON commands over USB serial; no network or second server required."""
import json
import os
import time

import serial
from serial.tools import list_ports


class WLEDSerial:
    def __init__(self, port=None, baudrate=115200):
        self.port = port or os.environ.get("BIOGLOW_WLED_PORT")
        self.baudrate = baudrate
        self.connection = None

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
            self.connection.reset_input_buffer()
            self._write({**params, "v": True})
            state = self._read_reply()["state"]
            if state.get("on") != params["on"] or (params["on"] and state.get("bri") != params["bri"]):
                raise OSError("WLED did not acknowledge the requested power and brightness")
            segments = state.get("seg", [])
            segment = next((item for item in segments if item.get("id", 0) == 0), None)
            if not segment or segment.get("col", [[]])[0][:3] != params["seg"][0]["col"][0][:3]:
                raise OSError("WLED did not acknowledge the requested color")
        except Exception:
            self.close()
            raise

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None
