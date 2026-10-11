# BioGlow VEML7700 LAN prototype

Both existing entry points now serve GET /reading and GET /health on default
port 8000, alongside the existing BirdCast and microphone routes.
Sensor access uses the Adafruit VEML7700 driver and Blinka ExtendedI2C(1),
address 0x10, gain 1/8, integration 100 ms, and a shared thread lock.
Health performs a real lux read. Failed reads return HTTP 503 with descriptive
JSON and no lux field. Initialization is retried after failure.

## Current verification

Debian 13.6, Python 3.13.5, Raspberry Pi 4 Model B Rev 1.5.
Hostname: NirRaspberryPi. This restricted session exposes only loopback
networking, denies socket creation, and exposes no /dev/i2c-1.
The boot configuration has #dtparam=i2c_arm=on commented out.
Sensor detection, dependency installation, live HTTP testing, LAN IP discovery,
and cover/uncover testing have NOT succeeded. No hardware values were simulated.
Eight socket-free unit tests passed, covering sensor failure, serialization,
both HTTP handlers, and existing scraper behavior. HTTP socket tests cannot
run in this session. The virtual environment exists but has no sensor packages
because pip could not retrieve them.

## Complete setup in a normal terminal on the Pi

First enable I2C through sudo raspi-config, Interface Options, I2C.
This is needed because bus 1 is absent and the boot setting is disabled.
If raspi-config requests a reboot, save your work before rebooting; a reboot
may be needed for the enabled controller to appear. No reboot or configuration
change has been performed by this implementation.

```bash
ls -l /dev/i2c-1
id
hostname
hostname -I
cd /home/nir/bioglow-birdcast
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

The user running the server needs read/write access to /dev/i2c-1.
If the device belongs to group i2c and nir is not in that group, add nir
with sudo usermod -aG i2c nir and log out and back in before proceeding.

Probe only the expected sensor and take a real reading:

```bash
cd /home/nir/bioglow-birdcast
.venv/bin/python -c 'from light_sensor import LightSensor; import json; print(json.dumps(LightSensor().read()))'
```

This command fails explicitly if the device at 0x10 does not respond.
Then run the cover/uncover check; stop any server before this separate hardware
test so only one process owns the sensor:

```bash
.venv/bin/python check_light_sensor.py
```

## Start, stop, and HTTP checks

Start the existing combined BioGlow server in the foreground:

```bash
cd /home/nir/bioglow-birdcast
.venv/bin/python /home/nir/bioglow-birdcast.py --port 8000
```

Stop: press Ctrl+C in that terminal. Run only one BioGlow entry point at a time.
The project entry point also works with .venv/bin/python birdcast_service.py
--port 8000. No startup service was added.

From another terminal on the Pi:

```bash
curl --noproxy '*' -i http://127.0.0.1:8000/health
curl --noproxy '*' -i http://127.0.0.1:8000/reading
```

Successful /reading JSON has lux, timestamp (UTC, ending in Z), and sensor
(VEML7700). /health returns 200 and sensor_connected true only on a successful
hardware read. Sensor failures return 503, sensor_connected false, and error.

From the Mac on the same LAN, try:

```bash
curl --noproxy '*' -i http://NirRaspberryPi.local:8000/reading
curl --noproxy '*' -i http://NirRaspberryPi.local:8000/health
```

Hostname URLs depend on local mDNS resolution and are not yet verified.
If mDNS fails, substitute the Pi LAN address reported by hostname -I:
http://<PI_LAN_IP>:8000/reading. The service binds 0.0.0.0 for LAN access.
No router port forwarding or internet exposure was configured.

Driver references: https://docs.circuitpython.org/projects/veml7700/en/latest/api.html
and https://docs.circuitpython.org/projects/extended_bus/en/latest/examples.html
