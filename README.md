# BioGlow BirdCast

Local Raspberry Pi dashboard for radar presence, light, microphone recordings,
optional animal identification, WLED lighting, and manual BirdCast snapshots.

Run commands from this repository. Start only one server:

```bash
.venv/bin/python birdcast_service.py --port 8000
```

Open `http://<Pi LAN address>:8000/`. Use `--no-animal-analysis` if the optional
model is not installed. Automatic microphone capture starts stopped after restart.
Lux sampling continues every second and presence logging every ten seconds without a dashboard client.

## Project map

- `birdcast_service.py`: HTTP service; `web/`: dashboard with no build step.
- `presence/`, `light_sensor.py`: sensor drivers and diagnostics.
- `lighting_control.py`, `wled_serial.py`: fixture state and USB communication.
- `collection_schedule.py`, `microphone_capture.py`, `recording_store.py`: capture lifecycle.
- `animal_*.py`: optional identification and common names.
- `reading_history.py`: permanent sensor and recording history queries.
- `test_*.py`, `tests/`: regression tests; `backups/`: local historical copies.
- `bioglow_light/`: separate fixture firmware/documentation project.

`recordings/`, `readings/`, `.runtime/`, `.models/`, virtual environments, logs,
bytecode, and local snapshot/config output stay outside version control.
Cleanup does not delete recordings, history, model assets, or historical copies.

## Communication and recovery

Dashboard reads time out after ten seconds; actions allow sixty seconds for
capture and scraping. A timed-out action may still finish on the server: refresh
its status before repeating it. Recording and deletion requests are not replayed
automatically. HTTP request headers and bodies have a ten-second socket timeout;
incomplete bodies are rejected.

Failed background lighting commands retry the newest desired state, including
manual on/off commands. Manual retries run at two-second intervals after an
attempt finishes; auto and distance modes retain their faster recovery loop.
USB errors close the port so the next attempt reconnects. With multiple USB
serial devices, set `BIOGLOW_WLED_PORT` to the controller's `/dev/serial/by-id/`
path. Radar uses a separate passive UART connection.

If the dashboard is unreachable, check the running process and port before
starting another server. `/v1/latest` returns cached/passive state; `/health`
explicitly reads sensors and returns 503 when hardware is unavailable. Stale or
unavailable radar is never interpreted as absence. See [presence/README.md](presence/README.md)
before changing UART settings or testing hardware.

History queries scan the complete log and bound sensor query memory by the
requested graph point limit. All-time queries require two scans. Recording
queries retain the latest event for each recording. Sampling is disclosed in
the response; text logs remain intact.

## Validation and detailed guides

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s presence -q
```

HTTP tests require local sockets; browser checks require Chromium and Node.
Synthetic tests do not verify wiring, UART signal quality, microphone sound, or
physical LED output. See [DASHBOARD.md](DASHBOARD.md), [LIGHTING.md](LIGHTING.md),
[LIGHT_SENSOR.md](LIGHT_SENSOR.md), and [ANIMAL_IDENTIFICATION.md](ANIMAL_IDENTIFICATION.md).
