#!/usr/bin/env python3
"""Stage an upload key without printing it or enabling the sender."""
import os
from pathlib import Path
import secrets


def main():
    root = Path(__file__).resolve().parents[1] / ".runtime"
    root.mkdir(exist_ok=True)
    pending = root / "telemetry-token.pending"
    if (root / "telemetry-token").exists():
        raise SystemExit("Telemetry is already configured; existing key was preserved.")
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Staged key already exists; preserved it.")
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(secrets.token_urlsafe(32) + "\n")
    print("Staged key in .runtime/telemetry-token.pending (mode 0600). Sender remains disabled.")


if __name__ == "__main__":
    main()
