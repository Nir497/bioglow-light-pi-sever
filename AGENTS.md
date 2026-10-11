# Repository Guidelines

## Project Structure & Module Organization

Work only in `/home/nir/bioglow-birdcast`, the repository root. `birdcast_scraper.py` fetches migration data; `birdcast_service.py` exposes the HTTP service. `presence/` contains the LD2410C UART receiver, parser, hardware check, and documentation. `light_sensor.py` implements the VEML7700 integration, currently disabled in the server because the sensor is disconnected.

The existing combined server entry point is `../bioglow-birdcast.py`; `../record_microphone.py` captures microphone audio. These external files are runtime dependencies; keep new work inside this repository. Tests live beside the project modules, in `presence/`, and in `tests/`. `recordings/` stores generated WAV files; `backups/` stores historical copies.

## Development & Test Commands

Run commands from `/home/nir/bioglow-birdcast`:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s presence -v
.venv/bin/python -m unittest test_combined_recording test_light_sensor -v
.venv/bin/python -m unittest test_birdcast_service test_birdcast_scraper -v
.venv/bin/python /home/nir/bioglow-birdcast.py --port 8000
```

These install dependencies, verify radar parsing and audio behavior, test service and scraper behavior, and start the combined server. Run only one server entry point. No build step is required.

## Coding Style & Naming Conventions

Use Python with four-space indentation, `snake_case` functions and variables, and `PascalCase` classes. Match surrounding code and use type annotations where established. Keep hardware access separate from parsing and HTTP response handling. No project formatter or linter configuration is established.

## Testing Guidelines

Use standard-library `unittest`; name files `test_*.py` and methods `test_*`. Cover malformed input, unavailable hardware, stale radar frames, and recording integrity. No numeric coverage threshold is configured. Socket tests require an environment permitting local HTTP connections. Synthetic frames and audio fixtures do not verify physical hardware.

## Commit & Pull Request Guidelines

Available history contains only `Initial commit`; no broader convention is established. Use concise, imperative commit subjects. Describe behavior changes, validation results, and hardware limitations in pull requests; link relevant issues when available. Commit and push task changes after each task, excluding unrelated files and generated recordings. Report failures explicitly.

## Hardware & Configuration

Consult `presence/README.md` before UART testing. Never treat unavailable radar data as absence. Preserve microphone downloads as `audio/wav`: ten seconds, mono, 48 kHz, 16-bit PCM. Keep services on the local network and avoid committing credentials, virtual environments, or private captures.
