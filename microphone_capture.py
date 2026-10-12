"""Capture five seconds of mono 48 kHz, 16-bit PCM from the station microphone."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave


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
    if args.source:
        environment["PULSE_SOURCE"] = args.source
    try:
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
    except (OSError, subprocess.SubprocessError, RuntimeError, wave.Error) as exc:
        print(f"Recording failed: {exc}", file=sys.stderr)
        return 1
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
