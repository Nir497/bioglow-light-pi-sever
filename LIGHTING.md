# WLED lighting on the existing dashboard

`birdcast_service.py` serves the dashboard and controls the ESP WLED controller
plugged into the Raspberry Pi by USB. Keep the existing server on port 8000;
no Wi-Fi connection, second server, or separate bridge is required.

Open **WLED light** (`/#lighting`) for the controls. The separate **Lux sensor**
page (`/#light`) contains only the VEML7700 readings and history graph:

- **On** restores the chosen manual brightness; **Off** switches off.
- Choose a color and brightness, then **Apply color & brightness**. Applying
  settings selects manual control, retaining Off if already selected.
- **Auto** restores lux control using the latest smoothed measurement.

Manual settings survive scheduled sensor reads, including lux sensor failures.
All dashboard clients share the server's settings. Restarting the server returns
to Auto with full manual brightness and warm orange as the initial color.
Polling preserves unapplied edits and focused controls.

## USB connection

The server uses the existing pinned `pyserial` dependency at 115200 baud and
WLED's JSON-over-serial interface. With exactly one USB serial device visible,
it selects that port and requests WLED identification before sending commands.
It does not select the radar's GPIO UART. If several USB serial devices exist,
set `BIOGLOW_WLED_PORT` to the WLED device's stable `/dev/serial/by-id/...` path
in the server environment, then restart the existing service.

The Pi must expose the USB controller's serial port and the server user must
have access to it. Use a data-capable USB cable. The connection is held open with
exclusive access and reset/boot control lines deasserted. The first query is
retried once on the same open port if the ESP is still booting. The response
parser accepts JSON following startup text, even without an intervening newline.
The page shows the selected USB serial path and baud rate; timeout errors show
whether the Pi received zero bytes or serial output without WLED JSON. No raw
serial log output is exposed by the dashboard. Unplugging or failed
acknowledgements closes the connection; subsequent commands or scheduled reads
retry discovery. No firmware, GPIO, LED-output bus, or configured LED count is changed.
Commands consolidate the strip into one segment covering all configured pixels.
WLED must have serial JSON support available on its configured serial pins.

The controller profile is the SP803E WLED-ESP32 driving the confirmed 3.2 ft, 144-pixel strip.
It uses WLED's native JSON-over-serial API, not the archived ESPHome endpoints.
Before each command, the server reads `info.leds.count` and current segments.
It expands segment 0 from `start: 0` to `stop: 144` (exclusive), sets solid
RGB with `grp: 1`, `spc: 0`, full segment brightness, zero offset, and unfreezes
output. Other active segments are removed so old spotlight or music layouts
cannot override the strip. Playlists and nightlight are stopped. The white
channel stays zero so the selected RGB color is preserved. Commands suppress
sync notifications and override incoming live streams. WLED's response must acknowledge power, brightness when on, and
color and the full segment range before the server reports success. The page
shows the firmware version, configured LED count, and last acknowledged pixel
coverage out of 144. If WLED reports fewer than 144 configured pixels, On and
color/brightness commands fail with a configuration message instead of silently
lighting a partial strip. Off remains available and discloses partial configured
coverage. Set the actual LED-output length to 144 in WLED LED Preferences if
needed; WLED's USB serial state API does not expose hardware bus configuration.
The server leaves data GPIO, bus type, voltage, and current limits unchanged.
This acknowledgement verifies controller state, not physical light output.

## Verification

Automated tests cover the separate page, On/Off/Auto requests, manual color and
brightness, 144-pixel bounds, spacing, frozen segments, overlapping segments,
configuration mismatches, unplugged USB, serial parsing, and acknowledgement
failures. Physical verification requires access to the Pi's USB device and
observation of the strip. On the WLED light page, check 144/144 coverage, apply
red/green/blue at moderate brightness, vary brightness, test Off/On, then select
Auto when finished. A reported coverage value alone cannot verify physical LED
wiring or light output.

## API and automatic behavior

`POST /v1/lighting` accepts one or more fields:

```json
{"mode":"on","brightness":128,"color":"#2288ff"}
```

`mode` is `on`, `off`, or `auto`; `brightness` is an integer 1–255; `color`
is `#RRGGBB`. Invalid requests are rejected before settings change. Use Off
for zero output; On remembers the manual brightness. Color/brightness without
a mode select manual control, preserving an existing Off state.

Commands run in one background worker with a single pending command. Slow or
unreachable lighting never blocks lux sampling or `/v1/latest`. The API reports
`pending` until WLED acknowledges, `ok` on success, and `controller_unavailable`
on failure. `/v1/latest` exposes cached lighting state without USB traffic.

Every lux read contributes to a 30-second rolling average. Below 30 lux the
fixture turns on; above 300 lux it turns off. Between those thresholds it
retains its on/off state. While on, automatic brightness increases from 50 at
30 lux to 255 at zero lux, with a three-second fade. Startup in the middle band
defaults to off. Unavailable lux retains the last output in Auto. Place the lux
sensor away from the fixture's direct light to avoid feedback.

`LightingControl` retains its optional HTTP transport for existing integrations;
the dashboard server now explicitly selects USB and never resolves
`bioglow.local` to control this device.

Protocol references: [WLED serial](https://kno.wled.ge/interfaces/serial/) and
[WLED JSON API](https://kno.wled.ge/interfaces/json-api/).

## Connection troubleshooting

The path is browser → `/v1/lighting` on the existing Pi server → USB serial
at 115200 baud → SP803E WLED firmware. It is not a direct browser serial session.
The first payload is `{"v":true}`, requesting WLED state and device information.
Lighting commands are sent only after the device identifies as WLED.

A response timeout means the serial port opened but WLED state was not received;
it does not prove the selected port is the controller or that the firmware
supports serial JSON. Check the selected device using the read-only host command:

```bash
.venv/bin/python -m serial.tools.list_ports -v
```

Run it from the repository on the Pi. If necessary, set `BIOGLOW_WLED_PORT` to
that controller's stable `/dev/serial/by-id/...` path in the existing server's
environment. Check WLED's serial speed and whether its RX/TX pins are allocated
by another function. WLED serial support must be enabled in the installed
firmware. ESPHome boot logs indicate different firmware and do not satisfy a
WLED handshake. Do not infer successful control from USB enumeration alone.
