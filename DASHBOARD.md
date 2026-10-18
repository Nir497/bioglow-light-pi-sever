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
there is no login. Keep the service on the local network. Everyone who can reach
it can view readings, download audio, fetch BirdCast, and record microphone audio.
No internet hosting or router forwarding is required. LAN reachability depends on
the host firewall and network; the hostname is not a verified reachability claim.

The Light page also provides WLED USB power, brightness, and color controls.
The controller plugs into this server's Raspberry Pi; no WLED Wi-Fi connection
or second server is needed. Manual settings stay active until Auto is selected
(or the server restarts). See [LIGHTING.md](LIGHTING.md) for USB port selection
and API details.

The sidebar provides Overview, Radar, Light, BirdCast, and Recordings. Opening a
page or refreshing the view loads saved results and the recording library, without
starting a microphone capture, light measurement, or BirdCast fetch. Choose
**Read now**, **Fetch BirdCast**, or **Record 5 seconds** to request new data.
The server automatically collects presence and light every ten seconds, even
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

New WAVs and their integrity metadata are stored in `recordings/`
under the selected server root. New captures are five seconds, mono, 48 kHz,
16-bit PCM. Recordings remain accessible after download, application cleanup,
and server restart until explicitly deleted. Each recording has a Delete recording
button with confirmation. Deletion removes audio, metadata, analysis, and its animal
history entries. Capture or active analysis returns a busy error; retry when it
finishes. There is no automatic retention timer. Existing captures
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
