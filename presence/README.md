# LD2410C on the existing BioGlow server

Implemented in /home/nir/bioglow-birdcast/presence/__init__.py and integrated into
both existing entry points. Default bind is 0.0.0.0:8000. Run one entry point only.
The repository entry point now also serves the LAN dashboard at `/`, with permanent
microphone recordings and manual sensor/BirdCast actions. See [DASHBOARD.md](../DASHBOARD.md).
Use `birdcast_service.py` for the dashboard; the external combined server has no dashboard.
The repository entry point birdcast_service.py enables the VEML7700 sensor:
/reading returns lux readings and /health includes light sensor health alongside
presence_sensor, serial_connected, and fresh. Health returns 503 when either
sensor is unavailable or radar data is stale. /presence reports radar readings.
The external combined entry point /home/nir/bioglow-birdcast.py still has the
light sensor disabled; use birdcast_service.py for light sensor readings.

## Verified and blocked, 2026-10-11

Pi 4 Model B Rev 1.5, Debian 13.6, Python 3.13. Boot config has enable_uart=1.
Boot cmdline has console=serial0,115200; running cmdline has console=ttyS0,115200.
Serial console is NOT disabled. This restricted session exposes no serial devices,
no host service bus, no LAN route, and denies HTTP socket creation. Actual
/dev/serial0 mapping, serial-getty state, existing running services, port availability,
sensor connection, and Mac connectivity cannot be verified here. pip installation
failed because no distributions could be fetched. No boot edits, system service
changes, or reboot were performed. Eight radar tests and four existing light tests
passed; inputs are synthetic/manufacturer examples, NOT live frames. Hardware
and live HTTP tests remain outstanding. Backups are in bioglow-backups and
bioglow-birdcast/backups with before-radar names.

## Required Pi setup (normal host terminal)

Before opening UART, physically confirm TX orange -> pin 10 GPIO15 RX,
RX yellow -> pin 8 GPIO14 TX, GND green -> pin 9, VCC blue -> pin 4 5V;
OUT disconnected. Software cannot establish physical connection without frames.

Inspect existing services/processes first:

```bash
systemctl list-units --type=service --all --no-pager
pgrep -af 'bioglow|birdcast'
ss -ltnp
ls -l /dev/serial0
readlink -f /dev/serial0
cat /proc/cmdline
systemctl status serial-getty@serial0.service serial-getty@ttyS0.service serial-getty@ttyAMA0.service
```

Disable the serial login console using sudo raspi-config, Interface Options,
Serial Port: login shell NO, serial hardware YES. This changes the boot command
line to remove the UART console while leaving UART enabled. It requires a reboot
for the running kernel console setting to change. Save work, then reboot yourself
when ready; the agent has not rebooted the Pi. After reboot, verify /proc/cmdline
has no serial console and the getty for the actual mapped device is inactive.
If a getty remains active, disable that specific serial-getty unit. Preserve console=tty1.
Use /dev/serial0, never guess its mapping. If mapped to ttyS0, verify the UART clock
settings and received frames before assuming 256000 baud works. No Bluetooth
or UART overlay changes have been made.

Check device permissions with ls -l and id. If the device's group is dialout and
nir lacks membership, sudo usermod -aG dialout nir then log out/in. Do not run the
HTTP service as root. Check that no other process owns the mapped UART with
sudo fuser -v /dev/serial0. Stop the existing bioglow process in its owning terminal
or through its identified service before restarting; do not kill unrelated processes.

## Dependencies and start/stop

A dedicated presence/.venv exists, but dependencies are not yet installed.
The combined server should use its existing .venv so light dependencies remain
available. pyserial was selected after checking its Linux serialposix implementation:
it supports custom baud via termios2/BOTHER, binary reads, timeout, and exclusive
access. The worker sends no radar commands or serial writes.

```bash
cd /home/nir/bioglow-birdcast
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
presence/.venv/bin/python -m pip install -r presence/requirements.txt
.venv/bin/python -m unittest discover -s presence -v
.venv/bin/python birdcast_service.py --port 8000
```

Stop with Ctrl+C. Worker shuts down and closes UART. No autostart service was
added. Legacy entry point (without dashboard): .venv/bin/python /home/nir/bioglow-birdcast.py
--port 8000. Port 8000 availability is unverified; if already occupied, identify
its owner first. Do not start a duplicate BioGlow service. --port accepts an
alternative port, but none has been chosen here.

## API

```bash
curl --noproxy '*' -i http://127.0.0.1:8000/presence
curl --noproxy '*' -i http://127.0.0.1:8000/health
curl --noproxy '*' -i http://127.0.0.1:8000/reading
```

/presence returns presence: none/moving/stationary/both; moving_distance_cm and
stationary_distance_cm are null for states without that target. Energy fields
moving_energy_score and stationary_energy_score are raw sensor scores 0–100,
not percentages or probabilities. timestamp is UTC receipt time of the latest
valid frame (the protocol has no sensor clock). fresh uses monotonic age <=2s
and an open serial connection. HTTP 503 includes an error, fresh:false, and the
last valid timestamp, but excludes presence, distances and energy values.
No data/stale data never means no person. An open port alone does not prove
sensor connection; only valid frames establish communication.

Parser checks binary header F4 F3 F2 F1, little-endian length 13, normal type
02, marker AA, states, energy range, closing 55 00, and footer F8 F7 F6 F5.
It buffers partial reports, parses concatenated reports and skips malformed
candidates one byte at a time, retaining partial headers. Only normal reports
are accepted. Engineering mode reports are ignored; mode is never changed.

Mac hostname URL: http://NirRaspberryPi.local:8000/presence
Health: http://NirRaspberryPi.local:8000/health
These are exact hostname URLs but are NOT verified reachable. No LAN IP can
be discovered in this session. Run hostname -I in a normal Pi terminal and use
the LAN address if mDNS fails. No internet exposure/router forwarding configured.

```bash
curl --noproxy '*' -i http://NirRaspberryPi.local:8000/presence
curl --noproxy '*' -i http://NirRaspberryPi.local:8000/health
```

## Required real hardware test

Stop the server first. In a normal terminal, run:

```bash
cd /home/nir/bioglow-birdcast/presence
.venv/bin/python check_hardware.py --connected
```

Please walk in front of the sensor, stand still, then leave the detection area
when prompted. The test writes actual received bytes to real-frames.bin and
prints timestamped readings. It refuses to overwrite an existing capture.
The sensor may report both during transitions. Its configured absence delay
can retain presence after departure; manufacturer factory default is 5 seconds,
actual current setting unknown. Detection range/sensitivity and environmental
motion affect results. HTTP freshness is independent of that delay. The worker
read timeout adds up to about 0.2 seconds. Do not claim success until valid real
reports and correlated movement/stillness/departure have been observed.
Then start BioGlow and repeat from the Mac using the curl commands.

Protocol source: manufacturer's LD2410C Serial communication protocol v1.00,
sections 1.2 and 2.3, mirrored at:
https://www.sudo.is/docs/esphome/components/ld2410/HLK-LD2410C_Serial_communication_protocol_v1.00.pdf
Manufacturer's product page lists v1.09, but that PDF link returned 404:
https://www.hlktech.com/en/Goods-239.html
pyserial source checked:
https://raw.githubusercontent.com/pyserial/pyserial/master/serial/serialposix.py


## Shared filtered range for radar and lighting

`range_estimate_cm` is the common range used by the radar map and distance-test
lighting. It follows a consistent moving/stationary channel, applies a 0.65-second
median and 5 cm deadband, and rejects jumps over 100 cm until they persist for
at least 0.8 seconds across three frames. The deadband allows crossings at the
100 cm and 600 cm LED endpoints. `range_filter_state` reports `tracking`,
`rejecting_jump`, `weak_signal`, or `no_target`.

A fresh raw no-target report clears range immediately. A stream gap over two
seconds resets range tracking; disconnected/stale responses still contain no
current distance. The debounced occupancy state may hold presence briefly even
when the current range is empty. Raw moving, stationary and detection distances
remain available for diagnostics and never replace an explicitly unavailable
filtered range on the dashboard.

During distance testing, `/v1/latest` returns the same sampled radar frame used
to compute the LED target. Lighting reports its source frame timestamp and
separates target from controller-confirmed position. Sensor sampling/history
cadence remains unchanged.
