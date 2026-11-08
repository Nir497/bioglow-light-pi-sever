"""Capture five seconds of mono 48 kHz, 16-bit PCM from the station microphone."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave


def pulse_environment(environment, source=None):
    """Find the user's audio server from a system service and reject output monitors."""
    environment = environment.copy()
    if not environment.get("PULSE_SERVER"):
        runtime = Path(environment.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        socket = runtime / "pulse" / "native"
        if socket.is_socket():
            environment["PULSE_SERVER"] = f"unix:{socket}"
    if not shutil.which("pactl"):
        raise RuntimeError("pactl is required to verify the microphone source; install pulseaudio-utils.")
    result = subprocess.run(["pactl", "--format=json", "list", "sources"],
                            capture_output=True, text=True, timeout=5, env=environment)
    if result.returncode:
        raise RuntimeError("Cannot connect to the microphone audio server. "
                           "Check that the user's PipeWire/PulseAudio service is running.")
    sources = json.loads(result.stdout)
    inputs = [item for item in sources
              if item.get("monitor_of_sink") in (None, 4294967295, "4294967295")
              and not item.get("name", "").endswith(".monitor")]
    requested = source or environment.get("PULSE_SOURCE")
    if requested:
        inputs = [item for item in inputs if item.get("name") == requested]
    if not inputs:
        raise RuntimeError("No microphone input is available. Connect or reconnect the microphone; "
                           "speaker output monitors cannot be used for station recordings.")
    environment["PULSE_SOURCE"] = inputs[0]["name"]
    return environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", default="pulse")
    parser.add_argument("--source")
    args = parser.parse_args(argv)
    if not shutil.which("arecord"):
        parser.error("arecord is required; install alsa-utils.")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(suffix=".wav", dir=output.parent)
    os.close(descriptor)
    environment = os.environ.copy()
    try:
        if args.device == "pulse":
            environment = pulse_environment(environment, args.source)
        elif args.source:
            environment["PULSE_SOURCE"] = args.source
        subprocess.run(["arecord", "-q", "-D", args.device, "-t", "wav", "-f", "S16_LE",
                        "-r", "48000", "-c", "1", "-d", "5", temporary],
                       check=True, timeout=15, env=environment)
        with wave.open(temporary, "rb") as recording:
            if (recording.getnchannels(), recording.getframerate(), recording.getsampwidth(),
                    recording.getnframes(), recording.getcomptype()) != (1, 48000, 2, 240000, "NONE"):
                raise RuntimeError("Microphone did not return five seconds of mono 48 kHz 16-bit PCM.")
        os.link(temporary, output)
        print(output)
        return 0
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, wave.Error) as exc:
        print(f"Recording failed: {exc}", file=sys.stderr)
        return 1
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
