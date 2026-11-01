# Repository Guidelines

## Project Structure & Module Organization

Work only in `/home/nir/bioglow-birdcast`, the repository root. `birdcast_service.py` is the current HTTP service and LAN dashboard entry point; `birdcast_scraper.py` fetches migration data. `web/` contains the dashboard HTML, CSS, and JavaScript, with no build step. `presence/` contains the passive LD2410C UART receiver, parser, hardware check, and documentation. `light_sensor.py` implements VEML7700 access; light collection is enabled and reports unavailable hardware explicitly.

`collection_schedule.py` owns automatic sensor sampling and optional microphone scheduling. `microphone_capture.py` captures five-second WAVs; `recording_store.py` validates, stores, downloads, and deletes recordings. `reading_history.py` manages timestamped text logs and graph queries. `animal_analysis.py` manages the optional local Perch queue; `animal_worker.py` performs inference, `animal_model.py` pins model assets, and `animal_names.py` resolves English common names using bundled names and cached iNaturalist lookups.

`../bioglow-birdcast.py` and `../record_microphone.py` are legacy external entry points/dependencies, still covered by compatibility tests. Keep new work inside this repository. Tests live beside the project modules, in `presence/`, and in `tests/`. `recordings/` stores WAVs and metadata; `readings/` stores text logs; `.runtime/` stores caches and analysis states; `.models/` stores model assets. These generated/private directories are ignored by Git. `backups/` stores historical copies. See `DASHBOARD.md`, `ANIMAL_IDENTIFICATION.md`, and `presence/README.md` for details.

## Collection, History & Dashboard Behavior

The repository server samples and logs presence every ten seconds and light every second, independently of dashboard clients. BirdCast stays manual. Radar still receives UART frames continuously; freshness requires valid frames within two seconds and an open connection. `/v1/latest` reads passive/cached state without adding log entries or measuring light. Explicit sensor reads may add measurements outside the scheduled cadence.

Automatic microphone recording starts stopped after each server restart. Start records immediately, then every 120 seconds; Stop prevents future captures while an active capture finishes. Scheduling is server-owned and shared by clients. Preserve capture locking, analysis pause/resume behavior, error reporting, and shutdown cleanup.

Text files contain one timestamped JSON record per line: `readings/presence.txt`, `luminance.txt`, `migration.txt`, and `recordings.txt`. Keep sensor history indefinitely. Log failed reads as unavailable, never zero or absence. Recording history includes only new recording events and new analysis results, without backfilling old results. Log analysis completion even when the dashboard is closed.

Graphs appear only on individual sensor pages and Recordings. Preserve metric/animal and time-range selectors, region filtering for migration, and gaps for unavailable values. Query the full log for time ranges; disclose sampling when limiting graph points. Automatic refreshes must preserve audio elements, open selectors, and focus.

Recordings persist until explicitly deleted. Dashboard deletion requires confirmation and removes the WAV, metadata, analysis state, corresponding animal-history entries, and cached recording references. Reject deletion during capture or active identification; prevent workers from recreating deleted data. Downloads and `finish_transfer()` do not delete recordings.

Animal predictions are unverified candidates. Show common names alongside scientific names where available, retaining scientific names as a fallback. Raw model scores have no fixed minimum or maximum, may be negative, and are not percentages or animal counts. New five-second recordings use one model window; retained ten-second recordings use two.

## Runtime Station Modes & Control Regimes

The server enforces explicit runtime station modes (`normal`, `direction_config`, and `manual_control`), defaulting to `normal` on startup. Modes isolate distinct control regimes, diagnostics, and calibration routines, preventing them from interfering with each other. By operating within an explicit mode, the server has permission to ignore certain sensor inputs or suppress automated behaviors, making outputs predictable and easy to reason about during debugging and configuration.

Station mode is managed through `GET` and `POST /v1/station-mode`, broadcast via `/v1/latest`, and selectable in the dashboard sidebar as well as through dedicated page toggles (such as the Directionality and WLED lighting switches). When a mode is not active, controls belonging to that regime (e.g. manual WLED lighting sliders and presets when not in `manual_control` mode) must be locked or greyed out in the interface to prevent inadvertent commands from overriding or competing with active station behavior.

## Development & Test Commands

Run commands from `/home/nir/bioglow-birdcast`:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s presence -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_dashboard test_dashboard_frontend test_birdcast_service test_birdcast_scraper test_light_sensor test_combined_recording -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_collection_schedule test_microphone_capture test_animal_analysis test_animal_names -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_dashboard_browser -v
.venv/bin/python birdcast_service.py --port 8000
```

Run checks relevant to the change. Frontend state tests require Node; browser tests require Chromium. HTTP and Chromium checks may require execution outside the sandbox. Optional animal analysis requires `requirements-animal.txt` and `setup_animal_model.py`; use `--no-animal-analysis` to run the server without identification. Run only one server entry point; the legacy combined server also uses port 8000. No build step is required.

## Cloudflare Tunnel & Favicon

The user authorized public dashboard access without a login through Cloudflare Tunnel. The verified public address on October 25, 2026 is `http://glow.ultraviolet.nirtech.org`; HTTPS returned a TLS handshake failure. Do not assume HTTPS works or replace working HTTP links without verifying the hostname's certificate setup. All existing dashboard controls are available to public visitors.

Preserve the existing Python server on port 8000 and LAN access. The remotely managed tunnel routes to `http://127.0.0.1:8000`. Cloudflare's official Debian ARM64 `cloudflared` package and boot-enabled `/etc/systemd/system/cloudflared.service` are installed. The service runs `cloudflared --no-autoupdate tunnel run --token-file /etc/cloudflared/tunnel-token`, waits for that file to exist, and restarts after failures. The root-owned token directory is mode 0700 and the token file must be mode 0600. Never print, commit, or include tokens in command arguments. `.runtime/cloudflared-token` is an ignored staging location; clear it after installing the protected token. See `CLOUDFLARE.md` for operations and UI steps.

Do not rerun `cloudflared service install`: the service already exists. Update its protected token file and restart the existing service. The tunnel service does not start the dashboard; the Python server currently runs separately in a login/SSH session. Server-side route changes require restarting that Python process. Restarting only the connector does not reload application code.

When giving SSH instructions, provide copyable commands and explicitly identify where each Cloudflare value comes from. The tunnel token is the long value after `service install` in Cloudflare's connector command, not the Tunnel ID. Explain when to paste it into a hidden `read` prompt; do not tell the user to replace the prompt text. Cloudflare's UI manages the published hostname and route.

`web/favicon.png` is the user-provided Glow image. `web/index.html` references `/glow-favicon.png?v=d359af34`; the static allowlist also serves `/favicon.png` and `/favicon.ico` as `image/png` from the same PNG. Preserve the correct MIME type and update the version when replacing the image. Static responses use `Cache-Control: no-store`, but browsers may retain favicon caches. Verify both local and public image responses before attributing failures to caching. Dashboard asset tests cover the favicon aliases and HTML link.

## Coding Style & Naming Conventions

Use Python with four-space indentation, `snake_case` functions and variables, and `PascalCase` classes. Match surrounding code and use type annotations where established. Keep hardware access separate from parsing and HTTP response handling. No project formatter or linter configuration is established.

## Testing Guidelines

Use standard-library `unittest`; name files `test_*.py` and methods `test_*`. Cover malformed input, unavailable hardware, stale radar frames, recording integrity/deletion, schedule start/stop and concurrency, durable history, and model-window handling. Verify dashboard updates preserve controls and audio. No numeric coverage threshold is configured. Socket tests require an environment permitting local HTTP connections. Synthetic frames, audio fixtures, and browser mocks do not verify physical hardware. Update mirrored service tests in `tests/` when changing behavior covered by the root service tests.

## Commit & Pull Request Guidelines

Use concise, imperative commit subjects. Describe behavior changes, validation results, and hardware limitations in pull requests; link relevant issues when available. Commit and push task changes after each task, excluding unrelated files, generated recordings, sensor logs, caches, models, and bytecode. Report failures explicitly.

## Hardware & Configuration

Consult `presence/README.md` before UART testing. Never treat unavailable radar data as absence. New microphone captures/downloads are `audio/wav`: five seconds, mono, 48 kHz, 16-bit PCM (240,000 frames; 480,044 bytes for the canonical WAV). Preserve support for retained ten-second WAVs and the legacy combined-server tests. Do not change external runtime files except the explicitly authorized Cloudflare package, connector service, and protected token configuration. Public access uses the authorized tunnel; do not add router port forwarding. Avoid committing credentials, virtual environments, or private captures.
