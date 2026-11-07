# Adaptive lighting implementation and deployment

Normal station mode starts in Auto, with the existing USB SP803E WLED ESP32
controller and 144 logical LEDs. Manual and direction calibration modes retain
their explicit sensor bypasses. Calibration spacing, grouping, beam width, offset,
color, saved preferences, collection schedules, history, and microphone startup
behavior are unchanged. No Pico or SPIKE code is used.

The requested `Bioglow lamp referance file.md` was unavailable; the user confirmed
it is on another computer and instructed use of WLED. Implementation follows the
repository's documented Pi → USB → WLED architecture instead.

## Calculation and configuration

`adaptive_lighting.calculate_brightness(is_dark, presence_detected,
person_distance_m, migration_level, config=None)` implements the reference pure
calculation, returning a rounded percentage. With default settings:

```
distance_target = 10 + 50 * (distance_m / 5)
final_target = min(distance_target, migration_cap)
```

Out-of-range, missing, negative, nonfinite, boolean and nonnumeric distances
produce zero. Darkness and valid presence are required. The separate stateful
policy implements filtering, holds, timeouts, and smoothing.

Edit `adaptive-lighting.json` in the repository and restart `bioglow.service`.
Unknown fields, malformed JSON, invalid ranges, and nonfinite settings prevent
startup rather than silently installing unsafe values. A missing file uses the
same defaults. This separate deployment configuration does not change the
version-1 portable lamp preference/backup format. It is not edited by dashboard
sliders and is not included in their backup.

| Parameter | Default |
| --- | --- |
| min_brightness / max_brightness | 10 / 60 percent |
| max_distance_m | 5 m |
| dark_lux / bright_lux | 15 / 25 lux |
| lux_stale_seconds | 3 s |
| presence_timeout_seconds | 2 s deployed (library fallback: 10 s) |
| off_fade_seconds | 0.5 s |
| smoothing_tau_seconds | 0.4481420118 s (0.2 new target per 100 ms) |
| lux_filter_tau_seconds | 1 s |
| migration_stale_seconds | 1800 s |
| unknown_migration_cap | 60% temporarily deployed; library fallback 10% |
| migration_region | US-CA |
| require_motion_to_activate | true |

Migration caps are the prototype policy constants: low 60%, medium 40%, high
20%, very_high 10%, and a configurable unknown cap (currently 60% for testing). They are **not scientifically validated
wildlife-safe limits**. Raising the configured maximum does not bypass these caps.

## Sensor acquisition and timing

The existing passive UART receiver continues receiving LD2410C reports. Every
100 ms, the lighting sampler reads its cached response and selects the nearest
valid active moving or stationary channel in range. It does not reopen UART,
wait for an HTTP client, or rely on a target's initial distance. Thus movement
changes the target continuously and stationary presence has no inactivity cutoff.
The current normal station rule requires confirmed movement in range to switch
on (`require_motion_to_activate: true`). Subsequent confirmed stationary
presence can keep it on. The existing radar estimator's `presence_confirmed`
flag gates Auto when present; raw unconfirmed clutter cannot start or reset the
hold. Live LD2410C responses always include this flag. Legacy integrations
without estimator fields retain their raw presence semantics.

A motion activation resets on daylight, invalid/stale sensors, a lighting-mode
change, or expiry of the presence-loss timeout. A stationary report returning
after that timeout cannot start a new session. Confirmed motion outside 5 m
cannot arm an in-range stationary channel. Set `require_motion_to_activate` to
false only if stationary-only initial activation is intentionally desired.
This setting was approved after the user confirmed the detection area was empty
while the radar reported a stationary target. It blocks stationary-only startup
false detections, but cannot distinguish genuine stationary presence from clutter
that persists continuously after an actual arrival. That still requires physical
radar sensitivity/placement calibration; no radar configuration commands are sent.
The dashboard exposes `presence_gate_reason`, `radar_presence_state`, raw nearest
channel distance and `motion_session_active` to make the decision inspectable.

Brightness uses a time-aware exponential filter, so repeated cached frames do
not accelerate convergence when scheduling varies. Normal WLED updates request
100 ms transitions; daylight, invalid/stale radar/lux, and cap decreases bypass
these transitions. Output bytes are floored to avoid exceeding percentage caps.

The LD2410C reports moving/stationary target ranges, not a list of identified
people. Selecting the nearest reported channel cannot guarantee the nearest of
all physical people, and the sensor cannot reliably distinguish humans from
animals. Its own target-reporting delay can precede the lighting timeout. Auto stops
renewing presence when the range filter reports `weak_signal` or `no_target`,
even if the occupancy estimator retains its debounced presence flag.
The separate distance-test page retains its existing filtered range and mapping.

A no-target, non-credible range, or out-of-area report starts a configured
two-second hold at the last
permitted **smoothed output**, then fades off within 0.5 seconds. The OFF fade uses elapsed time and a fixed
duration rather than a long exponential tail. Returning credible presence cancels the
hold. Migration decreases clamp the hold immediately; a later cap increase does
not brighten a held target. Radar failures, invalid distances, and stale frames
(two seconds maximum) bypass the hold and request OFF immediately.

Lux remains acquired and logged once per second by the existing VEML7700
sampler; the 100 ms controller checks its cached classification and freshness.
This preserves sensor history cadence and avoids redundant I2C traffic. A
one-second time-aware lux filter prevents noise from re-enabling darkness.
Below 15 filtered lux (and a raw reading below 15), classify DARK. A raw reading
above 25 immediately classifies BRIGHT and requests OFF. Between thresholds,
including endpoints, preserve the mode; startup/failure in that band stays OFF.
Invalid or stale lux clears the classification and requires a new confirmed
dark reading. Raw daylight has priority over filtering delay.

Shield or position the ambient sensor away from the strip. The application
cannot compensate for self-illumination without measuring the installation.
No wiring or sensor gain configuration was changed.

## Migration data: documented limitations

The existing BirdCast integration scrapes regional counts and records source
measurement time. The actual page also has an `isHigh` indication in embedded
page state; the public UI documents a **High** badge, based on comparisons of
live and historical observations. The scraper maps the badge in the migration
traffic section to `high`. It does not execute embedded JavaScript or apply
invented thresholds to total bird counts. Absence of the badge maps to `unknown`,
**not low or medium**. Very high is supported by the pure policy but is not
invented as a BirdCast dashboard classification.

The policy also requires a live-feed indicator, the configured region, and valid
source and retrieval timestamps no more than 30 minutes old. Future, unparseable,
missing, historical, failed, wrong-region or expired data selects unknown and
the configured unknown cap. Supported source timestamps have explicit US timezone abbreviations;
unrecognized formats fail conservatively. Old cached results do not become
fresh on service restart. A manual BirdCast snapshot updates the control cache;
there is no API query in the local loop. Without a documented category, the
prototype commonly uses the configured unknown cap even when counts can be displayed.

Provider references checked November 7, 2026:

- https://birdcast.org/migration-tools/migration-dashboard/ (High badge and live/historical modes)
- https://dashboard.birdcast.org/region/US-CA (actual provider page)
- https://birdcast.org/definitions-species-arrivals-regions-and-migration-amounts/
  (density classifications use different units; regional counts cannot be mapped to them)

## Optional controller watchdog firmware

**Stock WLED JSON brightness commands persist indefinitely after USB/Pi failure.**
The Pi cannot implement a controller watchdog after it has stopped running.
At the user's request, Auto also operates without BioGlow watchdog protocol v1.
The Pi continues applying all adaptive decisions and sensor fail-safe OFF
requests; stock WLED may retain its last brightness after complete command loss.
Off, manual control and direction diagnostics remain usable with stock firmware.
Manual/diagnostic commands explicitly disarm the adaptive watchdog; these modes
are intentional operator overrides, not the automatic safe regime.

`firmware/wled/usermod_bioglow_watchdog.h` and `command_watchdog.h` provide a WLED
usermod. They use WLED's existing LED bus; no GPIO, strip type, power budget, or
calibration is selected by this module. Startup and expiry request instant OFF.
A valid adaptive command contains `bioglow.watchdog_ms: 3000`. Repeated commands
refresh a local, wrap-safe millisecond deadline; missing commands expire it after
three seconds. No network clock or network connection is required. The Pi sends
an unchanged output heartbeat every second. Invalid JSON cannot refresh it.

To install on the observed WLED 16.0.1 controller, build the **matching WLED
firmware for this controller** with the `firmware/wled/` directory included as a
PlatformIO library. WLED 16 uses `custom_usermods` and `REGISTER_USERMOD`, not the
older `usermods_list.cpp` approach. This directory now includes the required
`library.json` and `bioglow_watchdog.cpp` registration unit.

In the WLED checkout, add `symlink:///home/nir/bioglow-birdcast/firmware/wled` to
the selected environment's `custom_usermods` in `platformio_override.ini`.
If building on another computer, copy this directory to `usermods/BioGlowWatchdog/`
and add `BioGlowWatchdog` instead. Preserve existing usermods in that list.
Use the controller's confirmed ESP32 board/flash/partition build profile; the
reported architecture alone does not establish the correct environment. The
legacy ESPHome YAML in this repository is not its current WLED build profile.

Example for a controller whose confirmed base environment is `esp32dev` (do not
assume this profile without checking its flash and partition layout):

```ini
[env:bioglow]
extends = env:esp32dev
custom_usermods =
  BioGlowWatchdog
```

After installing WLED's build prerequisites (VS Code with PlatformIO, Git and
Node.js), use `npm install` in the matching WLED source checkout, then build
with `pio run -e bioglow`, or select that environment's Build task in VS Code.
The resulting image is under `build_output/firmware/`; WLED's build process
names the firmware after the selected environment. Back up WLED configuration
and presets using Config → Security & Updates before uploading the compatible
image through WLED's firmware-update page. Do not erase flash or select a new
LED hardware layout as part of this update.

Preserve the controller's existing board/build profile, LED bus configuration and
power limit. Set WLED startup power OFF and disable boot presets/playlists before
acceptance testing; WLED can apply boot presets after usermod setup. The module
also suppresses output in its loop until an explicit Pi command, and suppresses
output again after expiry. These settings avoid a startup flash before the loop. Follow WLED's build instructions:
https://kno.wled.ge/advanced/compiling-wled/ and usermod instructions:
https://kno.wled.ge/advanced/custom-features/ .

The module advertises `info.bioglow_watchdog: 1`; adaptive acknowledgements must
also report `state.bioglow.watchdog_ms: 3000`. The USB transport enables and verifies the watchdog when the controller
advertises capability. Without the module it sends normal WLED commands and
reports watchdog unavailable. If the controller advertises the module but fails
to acknowledge arming, the transport still requests OFF and fails delivery. Hardware firmware has **not been built or flashed** by this
change; the platform watchdog core is compiled and simulated in tests. A full
matching-WLED build and physical power/USB-disconnect tests remain necessary.
The legacy HTTP transport also permits Auto, but does not verify watchdog
capability or arming; the active server continues to use USB.

Already-running serial exchanges cannot be preempted. Newest commands replace
queued commands; an older acknowledgement cannot overwrite a subsequent
brightness reduction in reported state. The independent Pi output worker also
fails dark on stale lux/radar if the sampler hangs. If delivery itself fails,
OFF is a request, not proof of physical darkness. The controller watchdog bounds
output after complete command loss when the module is installed and running.
Other WLED clients, macros and reboot presets must not compete with adaptive
control; physical testing must include the installed firmware's boot behavior.

## Dashboard and deployment

The existing WLED page and passive `/v1/latest` response expose raw and filtered
lux, ambient state, current nearest radar distance in meters, valid presence,
distance target, migration category and cap, final target, smoothed output,
last acknowledged brightness, lighting state, hold time remaining, freshness,
watchdog readiness, and separate sensor/migration/communication errors.
Acknowledged brightness is controller state, not measured optical output.
Polling updates existing text nodes and preserves forms, selectors and audio.

From `/home/nir/bioglow-birdcast`:

```bash
# Edit adaptive-lighting.json as needed. No dependencies or web build are added.
sudo systemctl restart bioglow.service
curl --noproxy '*' --max-time 3 -sS http://127.0.0.1:8000/v1/latest
```

Keep the existing service on port 8000; do not start another server. After a
restart, station mode is Normal/Auto, recordings remain stopped, live lux/radar
must become fresh, and migration defaults to unknown until a new manual fetch.
The dashboard shows watchdog unavailable with stock WLED; Auto still controls
lighting normally. Saved calibration is preserved.

Physical acceptance requires shielding the lux sensor; observing approach,
departure, stationary targets, the 5 m boundary and configured hold/fade; testing
bright-light and sensor failures; then disconnecting USB or stopping Pi commands
and observing controller OFF within three seconds. Test boot and reconnection
also. Simulated results cannot establish illumination, identification accuracy,
nearest-person coverage, glare, self-illumination, or wildlife safety.

## Changed files

Added: `adaptive_lighting.py`, `adaptive-lighting.json`,
`test_adaptive_lighting.py`, `firmware/wled/command_watchdog.h`,
`firmware/wled/usermod_bioglow_watchdog.h`, `firmware/wled/library.json`,
`firmware/wled/bioglow_watchdog.cpp`, and this document.

Modified: `lighting_control.py`, `birdcast_service.py`, `birdcast_scraper.py`,
`wled_serial.py`, `web/dashboard.js`, `LIGHTING.md`, `test_lighting_control.py`,
`test_birdcast_service.py`, its `tests/` mirror, `test_dashboard_frontend.py`, and `test_dashboard_browser.py`.
Obsolete Auto tests are replaced with the adaptive suite; manual, diagnostic,
calibration, serial and service regression checks are retained.

## Validation results (November 7, 2026)

Executed outside the sandbox where HTTP/Chromium require local sockets:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_adaptive_lighting test_lighting_control test_wled_serial test_birdcast_scraper tests.test_birdcast_scraper test_configuration_store test_dashboard_frontend test_birdcast_service tests.test_birdcast_service test_dashboard test_collection_schedule test_light_sensor test_dashboard_browser -q
```

200 tests ran: **199 passed, 1 unrelated existing failure**. The unchanged
`test_dashboard_assets_and_empty_data_never_probe_hardware` expects an explicit
`type="image/png"` in the homepage favicon tag; the current unchanged chooser
page omits this attribute. Adaptive tests, HTTP tests, Node frontend tests and
both Chromium tests passed. Initial obsolete Auto/threshold assertions were
updated for this implementation. The watchdog C++ deadline core compiled with
g++ (`-Wall -Werror`) and passed synthetic expiry, refresh, disable and millis
wraparound checks. The complete WLED firmware/user module remains unbuilt and
unflashed; these checks do not establish controller hardware behavior.

Restarted the existing `bioglow.service` and verified live `/v1/latest`:
Normal/Auto, approximately 287 lux, ambient BRIGHT (`light`), final/output and
acknowledged brightness zero, live radar frames (about 0.07 s age), USB WLED
16.0.1 acknowledging OFF, 144 configured pixels, and watchdog unavailable as
expected on stock firmware. Saved spacing 48, beam width 17, offset 0, manual
brightness 248, and color #606060 survived the restart. No physical movement,
visual alignment, lux shielding, nighttime activation, or USB-disconnect tests
were performed. No firmware or physical wiring was changed.

## Stock WLED compatibility update

The user subsequently authorized automatic operation without the watchdog module.
Auto now sends normal brightness commands to stock WLED and strips the optional
module field from USB commands when capability is absent. Installed modules are
still armed and their acknowledgements checked. The status field
`controller_watchdog_required` is false; watchdog availability remains visible.
The adaptive/WLED/lighting/Node regression run passed all 93 tests. No firmware
flash is needed for this compatibility mode.

## Confirmed movement activation update

The user approved requiring movement to switch on after confirming the detection
area was empty while live radar still reported confirmed stationary presence.
Auto now also honors the existing radar confirmation flag. The 148-test run
(adaptive, lighting, WLED serial, mirrored service tests and Node frontend)
passed. After restarting the existing service, live readings showed DARK at
10.2144 lux, fresh radar EMPTY/no target, motion activation false, and WLED
acknowledging zero output. No radar sensitivity or UART configuration was changed.
A physical walk-in/stillness/departure test is still required. Persistent
stationary clutter after a real arrival can still require radar calibration.

## Faster departure update

The user reported slow switch-off. Live diagnostics showed `PRESENT_STILL` and
`presence_confirmed: true` during the estimator's 4.5-second debounce, while
`range_filter_state: weak_signal`, `range_estimate_cm: null` and zero credible
frames indicated that no current credible range remained. Auto now treats
`weak_signal`/`no_target` range-filter states as presence loss, rather than
renewing the hold from a retained occupancy flag. This does not alter the radar
estimator, UART settings, or sensor history.

The deployed hold is now 2 seconds; the OFF fade has a configurable fixed
0.5-second duration. Tracking brightness retains its existing exponential
smoothing. The final zero command bypasses WLED transition delay. A brief signal
dip can still recover during the hold, and genuine credible stationary presence
remains active. The library fallback hold stays at 10 seconds for compatibility;
the checked-in deployment configuration selects 2 seconds. Physical sensor delay
or false detections that remain credible can still delay recognition of departure.

69 adaptive, lighting, and Node frontend tests passed, including held presence
with weak range, bounded OFF fade at variable update intervals, and recovery
from a brief weak signal. Physical elapsed time from a person leaving has not
been measured.

## Reversed distance curve

At the user's request, the distance request now increases with distance:
`min_brightness + (max_brightness - min_brightness) * distance / max_distance_m`.
With the current settings this is 10%, 20%, 30%, 40%, 50%, 60% at 0–5 metres;
beyond 5 metres cannot activate lighting. The nearest valid active target is
still selected. The movement activation, stationary tracking, two-second hold,
half-second OFF fade and migration caps are unchanged. In particular, the
unknown-migration 10% cap still masks visible distance changes. All 69 adaptive,
lighting and Node frontend tests passed after updating the curve and assertions.

## Temporary unknown-classification cap for distance testing

The user approved raising the unknown-classification cap to 60% so distance
changes are visible. `unknown_migration_cap: 60` in `adaptive-lighting.json`
applies to unavailable, stale, historical, wrong-region or unsupported migration
classifications. Recognized low/medium/high/very_high limits remain 60/40/20/10%.
The conservative library default stays 10%; restore `unknown_migration_cap` to
10 and restart the existing service to end this test configuration.

The site can display cached bird counts without a current classified lighting
input. At diagnosis it showed 1,382,800 birds from November 6, 2026, 5:30 p.m. PST
(an older cache predating classification fields). Counts alone are not an
intensity category, and the displayed provider-relative update text is part of
the cached response rather than a live age. No undocumented count thresholds
were introduced.

71 adaptive, lighting and Node frontend tests passed for the configurable
fallback, including full 20/40/60% targets at 1/3/5 m with unknown classification
and immediate enforcement of the unchanged High cap. A direct provider fetch
also returned the November 6 historical night (`live_feed: false`), recorded at
5:50 p.m. PST, without a supported classification. No dashboard snapshot was
silently refreshed or assigned invented migration levels.
