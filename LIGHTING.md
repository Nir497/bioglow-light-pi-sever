# WLED lighting on the existing dashboard

`birdcast_service.py` serves the dashboard and controls the ESP WLED controller
plugged into the Raspberry Pi by USB. Keep the existing server on port 8000;
no Wi-Fi connection, second server, or separate bridge is required.

Open **WLED light** (`/#lighting`) for the controls. The separate **Lux sensor**
page (`/#light`) contains only the VEML7700 readings and history graph:

- **On** restores the chosen manual brightness; **Off** switches off.
- Choose a color and brightness, then **Apply color & brightness**. Applying
  settings selects manual control, retaining Off if already selected.
- **Full brightness · 100%** selects manual On, applies the currently chosen
  color, and requests master brightness 255/255 and segment brightness 255/255.
- **Auto** uses adaptive lighting with 15/25 lux hysteresis, continuously updated
  nearest radar distance, a migration cap, a two-second presence hold and a half-second OFF fade.
  See [Adaptive lighting](ADAPTIVE_LIGHTING.md), including its optional WLED watchdog module.

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
channel stays zero so the selected RGB color is preserved. The primary color
is sent as `seg[0].col: [[R,G,B,0]]`; color bytes are never premultiplied by
brightness. The solid effect and default palette are explicitly selected.
Acknowledgement checks all four RGBW channels (a three-channel RGB reply implies
white zero), preventing an unwanted white channel from washing out the color. Commands suppress
sync notifications and override incoming live streams. WLED's response must acknowledge power, brightness when on, and
color and the full segment range before the server reports success. The page
shows the firmware version, configured LED count, and last acknowledged pixel
coverage out of 144. If WLED reports fewer than 144 configured pixels, On and
color/brightness commands fail with a configuration message instead of silently
lighting a partial strip. Off remains available and discloses partial configured
coverage. Set the actual LED-output length to 144 in WLED LED Preferences if
needed; WLED's USB serial state API does not expose hardware bus configuration.
The server leaves data GPIO, bus type, voltage, and current limits unchanged.
The page shows acknowledged RGB color, master brightness, segment brightness,
and WLED's reported current limit. Brightness 255 requests the full API range;
WLED's configured global brightness factor and automatic brightness limiter can
still reduce actual output. The application does not overwrite the power budget.
Physical color order remains the strip's WLED LED Preferences setting; if red
and green are physically swapped, correct the output's color order there.
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

`mode` is `on`, `off`, `auto`, or `distance_test`; `brightness` is an integer 1–255; `color`
is `#RRGGBB`. Invalid requests are rejected before settings change. Use Off
for zero output; On remembers the manual brightness. Color/brightness without
a mode select manual control when brightness is included, preserving an existing
Off state. Color-only changes preserve Auto.

Commands run in one background worker with a single pending command. Slow or
unreachable lighting never blocks lux sampling or `/v1/latest`. The API reports
`pending` until WLED acknowledges, `ok` on success, and `controller_unavailable`
on failure. `/v1/latest` exposes cached lighting state without USB traffic.

Auto uses the server-owned lux sampler and a 100 ms loop reading the existing
passive radar receiver. Brightness increases as the nearest reported active
moving or stationary channel approaches (60% at 0 m, 10% at 5 m). Targets outside
5 m cannot activate output. Confirmed movement in range is required to switch
on; subsequent confirmed stationary presence keeps output active. Unconfirmed
radar clutter cannot activate or renew the hold. The migration
cap never increases the distance request; missing or unsupported classifications
use 10%. Daylight and failed or stale sensors request immediate OFF. Manual and
direction diagnostics retain their explicit overrides.

See [ADAPTIVE_LIGHTING.md](ADAPTIVE_LIGHTING.md) for the configurable settings,
filtering, timeout/fade, provider classification limitations, dashboard fields,
controller watchdog firmware installation, test results and hardware acceptance
procedure. Auto works with stock WLED. The optional BioGlow watchdog adds controller-side
OFF after command loss; stock WLED may retain its last brightness if the Pi stops. The existing USB connection,
LED rendering/calibration and power budget are preserved. Acknowledgement is
controller state, not proof of optical output.

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
