# BioGlow LAN dashboard

Run the repository server as the single BioGlow server, from this directory:

```bash
.venv/bin/python birdcast_service.py --port 8000
```

Stop the previous combined server in its owning terminal or service before starting
this server. Both entry points bind port 8000; do not run both. No system service
or boot configuration is changed by this feature.

Open `http://NirRaspberryPi.local:8000/` from a device on the same network, or use
`http://<Pi-LAN-IP>:8000/` if mDNS is unavailable. The default bind is `0.0.0.0`;
there is no login. Everyone who can reach
it can view readings, download audio, fetch BirdCast, and record microphone audio.
For public HTTPS access through Cloudflare Tunnel, see
[CLOUDFLARE.md](CLOUDFLARE.md). No router forwarding is required. LAN reachability depends on
the host firewall and network; the hostname is not a verified reachability claim.

The separate **WLED light** page (`/#lighting`) provides SP803E WLED-ESP32 USB
power, brightness, and color controls plus coverage of the 192-pixel strip.
The **Lux sensor** page (`/#light`) displays calculated lux, raw ambient-light
counts, and raw white-channel counts, along with gain, integration time, and
lux-per-count resolution. Its graph selector includes all three light readings.
Raw channels are 16-bit counts, not separate lux measurements. All fields are
saved with scheduled reads; older history entries lack the added channels and
appear as gaps when those channels are selected.
The controller plugs into this server's Raspberry Pi; no WLED Wi-Fi connection
or second server is needed. Manual settings stay active until Auto is selected
(or the server restarts). See [LIGHTING.md](LIGHTING.md) for USB port selection
and API details.

The sidebar provides Overview, Radar, Lux sensor, WLED light, BirdCast, and Recordings. Opening a
page or refreshing the view loads saved results and the recording library, without
starting a microphone capture, light measurement, or BirdCast fetch. Choose
**Read now**, **Fetch BirdCast**, or **Record 5 seconds** to request new data.
The server automatically collects lux every 250 ms and presence every ten seconds, even
with the dashboard closed. BirdCast remains manual. On Overview or Recordings,
choose **Start recording every two minutes** to capture immediately and repeat
five-second microphone captures every 120 seconds. **Stop automatic recording**
prevents future captures; an active capture finishes. Automatic microphone
recording starts stopped after each server restart. Collection status, errors,
and the next recording time appear beside these controls.
Radar is a passive receiver: reading it displays its newest frame rather than
sending a command to the sensor. Stale radar data is unavailable, never absence.
See `presence/README.md` before UART testing. Light may be unavailable while the
VEML7700 is disconnected. API errors are shown alongside previous successful light
or BirdCast results explicitly labeled as historical data.

Microphone capture uses `arecord` (`alsa-utils`). The default `--device auto`
selects a USB ALSA capture device using its stable card ID, such as
`plughw:CARD=Device,DEV=0`, so a USB microphone works even when PulseAudio has not
exposed it. ALSA converts the input to the required mono 48 kHz PCM format.
If no USB capture device exists, the recorder uses PulseAudio and verifies inputs
with `pactl` (`pulseaudio-utils`). It discovers the service user's socket at
`/run/user/<uid>/pulse/native` when `PULSE_SERVER` is unset. Speaker output monitors
are rejected. Explicit `--device` values override discovery; `--source` or
`PULSE_SOURCE` selects PulseAudio when the device is auto. Microphone failures
leave automatic lighting on its existing brightness rules.
The microphone is optional. Automatic lighting checks input availability every
ten seconds and bypasses retained sound classifications while no input is
available. Reconnecting the microphone is detected without restarting the server.
Recording attempts still report an error when no input is available; they never
create substitute silent recordings. Sensor sampling continues independently.

New WAVs and their integrity metadata are stored in `recordings/`
under the selected server root. New captures are five seconds, mono, 48 kHz,
16-bit PCM. Recordings remain accessible after download, application cleanup,
and server restart, with automatic deletion after five days. Each recording has a Delete recording
button with confirmation. Deletion removes audio, metadata, analysis, and its animal
history entries. Capture or active analysis returns a busy error; retry when it
finishes. Automatic cleanup runs at startup and every minute, even with microphone
recording stopped. Busy recordings are retried on the next sweep; expiration uses
the original capture timestamp and applies to existing recordings too. Existing captures
in `/home/nir/recordings/` are not imported. Invalid/incomplete files and symlinks
are omitted from the library. Avoid modifying retained WAVs or their sidecar JSON:
changed captures fail their SHA-256 integrity check.

Optional local Perch analysis identifies species candidates after each new
recording. Older recordings can be analyzed with **Identify animals**. See
[ANIMAL_IDENTIFICATION.md](ANIMAL_IDENTIFICATION.md) for setup, API, measured Pi
performance and limitations. Suggestions do not count individual animals.

Overview and Radar refresh the passive radar reading every second while visible.
Refreshes update only the radar card, preserving audio playback. The page still
marks expired frames unavailable if the sensor stops reporting or requests fail.

Each API read appends a timestamped JSON line to `readings/presence.txt`,
`readings/luminance.txt`, or `readings/migration.txt`. Presence reads include
the automatic ten-second sampler and explicit `/presence` reads. `/v1/latest`
only reads passive/cached state and does not log extra samples. Explicit light
reads and light health checks also log their measurement.
Reading saved history does not repeat light measurements or migration fetches.
Sensor logs retain all entries across restarts and are excluded from Git. Failed reads
are logged as errors, never as zero or absence. Logging errors appear in the
response as `history_error` and in server logs.

Graphs appear only on Radar, Light, BirdCast, and Recordings pages. Select a
metric and a time range: half hour, hour, 6 hours, 12 hours, day, week, or all time.
The history endpoint queries the full text log rather than the latest 500 entries.
For large ranges, it plots the last reading per time bucket (up to 1,000 points),
with sampling disclosed below the graph; full logs are preserved. Migration graphs
use the selected region. Missing values and failed readings leave gaps among the
plotted readings. Hover or focus points for values and timestamps. Graphs refresh
every ten seconds and offer Refresh graph. Live radar refreshes preserve selectors.

`readings/recordings.txt` logs new recording events and new animal-analysis results,
including recording timestamp, analysis timestamp, and each returned animal's
scientific name and raw score. All animals returned across the analysis windows are
kept with their maximum score. No existing results are backfilled. Analysis
completion is logged even with the dashboard closed. The Recordings graph uses
the latest logged result per recording and plots the selected animal's raw score
at recording time. An animal missing from a result has no score, never zero.
Unverified model scores have no fixed minimum or maximum and are not percentages.
Common names from iNaturalist appear alongside scientific names; seed names and
cached lookups work offline, and unknown names retain their scientific name.

Latest successful light/BirdCast results, microphone outcomes, and last request
errors are cached in `.runtime/latest.json`. Radar state is kept by the existing
receiver and re-evaluated for freshness on each view request. Cached results do
not claim to be live. Scraper temporary config/output now live in `.runtime/`, so
requests do not overwrite the repository's existing snapshot or config files.
Runtime data and recordings are ignored by Git.

## API additions and compatibility

- `GET /` serves the dashboard; `/dashboard.css` and `/dashboard.js` serve its assets.
- `GET /v1/latest` returns `sources` and supported `regions`. Cached sources include
  `result`, `http_status`, `requested_at`, and `last_success`; radar includes its
  current passive receiver result and status. This endpoint does not probe hardware.
- `GET /v1/recordings` returns `recordings` sorted newest first, with timestamps,
  WAV parameters, lengths, hashes, and download links.
- `POST /v1/recording-schedule` accepts `{"enabled": true}` to start or
  `{"enabled": false}` to stop periodic captures. `/v1/latest` includes
  `collection` with enabled/recording state, next capture time, intervals,
  duration and errors. Scheduling is owned by the server, shared by all clients.
- `GET /v1/history?source=radar&range=1h` returns bounded plot entries, total count,
  time-axis bounds, sampling status, and animal choices. Sources: `radar`, `light`,
  `birdcast`, `microphone`. Ranges: `30m`, `1h`, `6h`, `12h`, `1d`, `7d`, `all`.
  BirdCast accepts `region=US-CA`. This endpoint does not read sensors or log a reading.
- `GET /v1/animal-names?name=Bubo%20virginianus` returns English common names for
  up to 30 scientific names. Exact scientific names or exact scientific synonyms
  are matched against iNaturalist; unrelated search matches are rejected.
- Existing `/presence`, `/reading`, `/health`, `POST /v1/snapshots`, and
  `POST /v1/recordings` remain available. Recording creation returns HTTP 200 as
  in the repository server. Metadata includes both `download_path` and
  `download_url`, plus aliases for external combined-server WAV parameter fields.
- `GET /v1/recordings/<id>` repeatedly serves validated `audio/wav` with
  `X-Content-SHA256`. Downloads do not block a new capture.
- `DELETE /v1/recordings/<id>` permanently deletes the WAV, its metadata and
  analysis state, and its recording/animal-history entries. It returns
  `{ "status": "ok", "recording_id": "...", "retained": false, "deleted": true }`.
  Missing recordings return 404; active capture or identification returns 409.
  Downloads and `finish_transfer()` retain recordings. Legacy clients that used
  DELETE only to acknowledge a download must stop calling it to preserve audio.

## Checks

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_dashboard test_dashboard_frontend test_birdcast_service test_birdcast_scraper test_light_sensor -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s presence -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

`test_combined_recording` tests the unmodified external server, not this dashboard.
Mock recordings and synthetic radar frames verify behavior, not physical hardware.

The optional frontend state check uses an installed Node binary and a minimal DOM
double; it does not require npm packages. It verifies cached loading, manual actions,
navigation, safe text rendering, and local radar expiry, not browser layout or media
playback. Headless Chromium attempts in this session timed out without producing a
page or screenshot. Desktop/mobile visual checks, live microphone playback, and LAN
access from another device remain to be verified on the host.


### Automatic distance brightness

Auto (the startup default, API `{"mode":"auto"}`) controls all 192 LEDs using the
dashboard color. At 1 m or closer brightness is 15% (38/255); between 1 and 6 m
it rises linearly, reaching 50% (128/255) at 6 m or farther. At 3.5 m it is
83/255. Changes fade over 0.3 seconds. No target, stale/unavailable radar, or an
unusable filtered range requests immediate off. Startup requests off until fresh
radar and confirmed darkness arrive. Controller failures can prevent a command from reaching the strip.

Auto and Distance test share the half-second radar loop and filtered range below.
The lighting page refreshes every half second and shows sampled distance, target
and acknowledged brightness, and separate sensor/controller errors. Auto permits
distance control at <10 lux and requests off at >30 lux, retaining the previous
ambient classification between them. Startup in that band stays off. Failed,
invalid, missing, or more than 30-second-old lux keeps Auto off; a fresh threshold
reading and new radar sample are required to recover. The latest lux controls
this gate, independently of the rolling average. The WLED page shows lux, age,
ambient state, fixed thresholds, and the gate reason. Manual On and Distance test
bypass the gate. Lux is collected and logged every 250 ms on its own scheduler thread; slow presence reads do not delay it.
The Lux sensor page refreshes cached readings every 250 ms. Collection status
reports `light_interval_seconds: 0.25` and `sensor_interval_seconds: 10` (presence).
Color changes preserve Auto; manual brightness is disabled in Auto. On restores
saved manual brightness. Shutdown requests off for both radar-controlled modes.
USB acknowledgments verify full-strip coverage, color, and brightness, clear
competing segments, and preserve controller power limits.

### Distance test lighting mode

Select **WLED light → Distance test** to activate testing and open its dedicated
`/distance-test` page. This page combines live radar readings, raw diagnostics,
and LED output status, refreshed together every half second. The sidebar and
lighting page also link directly to it; opening the URL alone does not activate
the lights. Use **Start distance test** or **Off · stop test** on that page.

The mode tests the LD2410C distance sensor with one LED. The server checks the existing radar receiver every 0.5 seconds,
even with the browser closed; sensor history keeps its normal 10-second cadence.
LED 1 lights at 1 m or closer; LED 192 lights at 6 m or farther.
Between 1–6 m, distance maps linearly onto the strip, constrained to LEDs 2–143
so rounding cannot light an endpoint before its threshold (API indices 0–143). The dot stays amber (`#ffa028`) at 25% brightness
(64/255). This development mode tests position independently of Auto brightness.

The LED and radar map use the same `range_estimate_cm` and the exact same sampled
frame while testing. Raw moving/stationary distances remain visible as diagnostics;
they do not independently move the LED. The page labels the **Target LED** and the
last controller-**Confirmed** LED separately, so slow USB acknowledgments are visible.

The shared radar range follows the nearest consistent moving/stationary channel,
uses a 0.65-second median and a 5 cm deadband, and requires a jump over 1 m to
persist for at least 0.8 seconds and three frames before accepting it. Alternating
near/far spikes therefore retain the current range; sustained relocation still
changes it. A gap over two seconds resets tracking. Weak signals without usable
range do not fall back to raw distances in the UI or LED code.

Unchanged LED commands are skipped for up to two seconds, with periodic reassertion
and immediate updates when the target changes or becomes unavailable. USB commands
still require an acknowledgment; discovery/configuration queries are reused for five
seconds and invalidated on connection errors or reported configuration changes.
Radar estimation classifies each frame once per evaluation and publishes its result
under a short lock; the half-second lighting loop uses monotonic deadlines.

The first fresh no-target report turns the strip off. The radar's configured
absence delay still applies. Stale/unavailable readings or a target with no usable
distance request lights off and show a separate sensor error; fresh readings
resume tracking. Controller failures appear separately and may prevent an off
command from reaching the strip.

On, Off, and Auto exit the test. On/Auto restore full-strip lighting; appearance
controls retain their normal settings and are disabled during testing. Test mode
is not restored after restart. Shutdown attempts to turn radar-controlled lights off.

`POST /v1/lighting` accepts `{"mode":"distance_test"}`. Lighting status includes
`distance_cm`, zero-based `led_index`, `sample_interval_seconds`, `test_state`
(`waiting`, `tracking`, `no_target`, or `sensor_unavailable`), `test_brightness`,
`test_color`, `distance_source`, `radar_timestamp`, `confirmed_led_index`,
`confirmed_distance_cm`, `confirmed_radar_timestamp`, and an optional `sensor_error`
while this mode is active. `/v1/latest` pairs the lighting target with its sampled
radar entry; it does not read a second, newer frame during Auto or distance testing.

For hardware validation, enable the mode, walk from under 1 m toward 6 m or farther, stand
still, then leave. Confirm exactly one LED moves along the strip, departure turns
it off after the radar's delay, and On restores the entire strip. Automated tests
use synthetic radar readings and mocked controller acknowledgments.

For Auto hardware validation, confirm darkness at <10 lux, then approach, move away, stand still, and leave the detection area. Verify whole-strip brightness, selected color, five-second inactivity, and off after the radar absence delay. Confirm >30 lux forces off and lux sensor failure keeps Auto off. Check that the strip itself does not illuminate the lux sensor enough to cause cycling; sensor placement is not yet verified.

Auto output delivery retains the latest desired command independently of radar
sampling. The output worker retries failed commands every 0.5 seconds after a
request completes. Distance test reasserts acknowledged output every two seconds. A separate
output watchdog requests immediate off if radar sampling stops for over two
seconds; malformed readings report a sensor error without terminating sampling.
Slow USB acknowledgments do not block the radar sampling thread. Status includes
`radar_loop_running`, `sample_age_seconds`, and `ack_age_seconds`; the lighting
page displays sample and acknowledgment ages. Delivery still depends on a working
controller connection; a blocked request must finish or time out before off can
be sent.

While darkness is confirmed, Auto wakes on the first usable target or a change of at least 5 cm in the
filtered range. After five seconds without such a change it turns off, even if
radar continues detecting a stationary wall. Fresh timestamps, raw channel
noise, and color changes do not reset this timer. Movement reactivates Auto.
Successful unchanged Auto commands are no longer periodically resent; failed
commands still retry. Distance test retains its periodic reassertion, and manual
On retains its saved brightness and full brightness option.

## WLED command log

Open **WLED log** (`/#wled-log`) for a live, newest-first list of USB commands,
queries, replies, unexpected serial output, and communication errors observed by
this server. Each event includes its timestamp, direction, and full payload.
Partial or uncertain writes are labeled; a completed write alone does not prove
controller acknowledgment. Received fragments appear once, as read.

The server retains the latest 2,000 events in memory even when the page is closed;
restarting the server clears the log. The page polls every 500 ms while visible,
preserves text selection and scroll position, and reports expired history.
Viewing it never sends controller commands. This does not monitor other apps,
Wi-Fi clients, or serial data the server has not read.

`GET /v1/wled-log?after=0` returns events after the given nonnegative event ID,
plus `session`, `latest_id`, `oldest_id`, `expired`, and `reset`. A cursor beyond
the current sequence returns all retained events with `reset: true`; clients
must also detect changed sessions across server restarts. Invalid cursors return
HTTP 400. Incoming events include `bytes_hex` to preserve original bytes when
serial text contains invalid UTF-8. No log files are written.

### Settings persistence and portable backups

Lighting preferences are saved automatically in the ignored
`.runtime/configuration-v1.json`: spiral spacing, beam width, offset, manual
brightness, calibration brightness, and RGB color. Writes use a serialized
settings transaction, file fsync, atomic replacement, and directory fsync. The
old `.runtime/lamp_config.json` is never read. Missing files use defaults;
invalid files are preserved and a visible error blocks ordinary saves until
an explicit backup restore replaces the file.

Startup loads preferences but always enters Normal / Auto with microphone
scheduling stopped. Enter Direction Config to reuse calibration. Rendering
continues to use WLED segment grouping, spacing, and offset.

Configuration → Settings & backup downloads a readable, versioned JSON snapshot
of accepted shared preferences and this browser's display choices. Uploads are
fully validated, preview shared and browser changes separately, and require
Confirm restore. A changed server revision (including a server restart)
invalidates the preview. Cancel has no effect. Restore preserves station mode
and recording state; controller delivery errors are separate from durable saving.
Backups exclude sensor logs, recordings, results, credentials, and runtime state.
These controls retain the dashboard's existing public access policy.

Region, each graph's metric and range, and the selected animal live in
`localStorage` under `bioglow-display-v1`. They belong to that browser/origin.
Unavailable storage displays a warning; obsolete selections fall back to valid
choices. Backup import changes only the importing browser.

API: `GET /v1/configuration` returns shared preferences, an opaque revision,
and any startup error. `POST /v1/configuration/preview` accepts
`{"backup": ...}`; `POST /v1/configuration/restore` accepts the complete backup
and the preview's `revision`. Restore returns HTTP 409 when the preview is stale.
Successful appearance edits and restore mean preferences were saved even if
the returned lighting status reports a disconnected controller.

Verified November 1, 2026: the 182-test regression run passed, followed by
two passing Chromium tests including two independent profiles across reloads.
Actual service restart checks retained 51/1/0 and 49/3/7 calibrations, manual
brightness/color, and confirmed backup restoration including calibration
brightness. The captured original 50/5/4 calibration was then restored, with
Normal/Auto startup and recording scheduling stopped. The baseline DOM mock
was repaired so frontend checks run. Direct live Chromium navigation remained
blocked at a blank page in this environment; browser UI and profile checks used
the existing synthetic HTTP fixtures. Physical alignment remains a visual check,
and a full operating-system reboot was not performed.

The physical lamp has 192 LEDs over 3½ spiral wraps (confirmed November 1, 2026).
USB commands cover indices 0–191, including the final 48 LEDs previously omitted.
Existing spacing, beam width, offset and persisted preferences retain their values
and constraints; spacing is independent of total strip length.
