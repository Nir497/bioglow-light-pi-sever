# Automatic BioGlow lighting

The existing `birdcast_service.py` owns automatic lighting independently of
dashboard clients. The Light page shows luminance and lighting together.
Run the existing server without starting `bioglow_light/server.py` separately.

On the Light page, **On** selects full brightness (255), **Off** switches
the fixture off, and **Auto** restores lux control immediately using the
latest smoothed reading. Manual selection is shared by all clients and
survives scheduled reads, including sensor failures. Restarting the server
returns to Auto. `POST /v1/lighting` accepts `{"mode":"on"}`, `off`, or `auto`.

Every sensor read contributes to a 30-second rolling average. Below 30 lux
the fixture turns on; above 300 lux it turns off. Between those thresholds
it retains its on/off state. While on, brightness increases linearly from
50 at 30 lux to 255 at zero lux, with a three-second fade. Startup in the
middle band defaults to off. Unavailable lux retains the last output.
Place the sensor away from the fixture's direct light to avoid feedback.

The implementation uses the base project's warm color recipe and ESP32 LAN
control architecture. The live controller at `bioglow.local` runs WLED
0.14.4 and reports 100 RGB pixels, rather than the archived project's
ESPHome 144-pixel RGBW setup. No firmware replacement or LED-count change
is needed. `lighting_control.py` uses WLED's `/json/state` API and keeps the
configured segment geometry. Segment 0 uses solid warm RGB (255,160,40),
with a white channel value for RGBW configurations. Brightness is 0-255.

Commands suppress outgoing sync notifications and override incoming live
streams until the fixture reboots. Each valid reading reasserts the command,
including after a restart. Failed requests are shown on the dashboard and
retried on the next valid read. Displayed brightness is the last acknowledged
command, not physical feedback. `/v1/latest` exposes lighting state without
reading lux or sending fixture commands.

The fixture display name is **BioGlow** and its mDNS hostname is **bioglow**,
available at `http://bioglow.local/`. These are fixture settings; the Pi keeps
its existing hostname and dashboard port 8000.

Protocol reference: https://kno.wled.ge/interfaces/json-api/
