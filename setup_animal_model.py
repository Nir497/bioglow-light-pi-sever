#!/usr/bin/env python3
"""Download and checksum pinned Perch assets explicitly, never during HTTP requests."""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from urllib.request import urlopen

from animal_model import (IDENTITY, INT8_FILE, INT8_SHA256, LABEL_SHA256, MODEL_FILE, MODEL_SHA256,
                          REVISION,
                          check_assets, checksum, read_labels)
from birdcast_scraper import atomic_write_json


def setup(directory: Path, int8_comparison: bool = False) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    files = [(MODEL_FILE, MODEL_SHA256), ("labels.txt", LABEL_SHA256)]
    if int8_comparison:
        files.append((INT8_FILE, INT8_SHA256))
    for filename, expected in files:
        target = directory / filename
        if target.is_file() and not target.is_symlink() and checksum(target) == expected:
            continue
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".download-")
        try:
            url = f"https://huggingface.co/tphakala/Perch-v2/resolve/{REVISION}/{filename}"
            with os.fdopen(descriptor, "wb") as output, urlopen(url, timeout=60) as source:
                while block := source.read(1024 * 1024):
                    output.write(block)
            if checksum(Path(temporary)) != expected:
                raise ValueError(f"Downloaded checksum mismatch: {filename}")
            os.replace(temporary, target)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()
    read_labels(directory)
    atomic_write_json(directory / "manifest.json", IDENTITY)
    check_assets(directory, verify=True)
    print(f"Verified Perch model and 14,795 labels in {directory}; great horned owl supported.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path,
                        default=Path(__file__).resolve().parent / ".models" / "perch-v2")
    parser.add_argument("--int8-comparison", action="store_true",
                        help="Also download the 131 MB INT8 model for offline comparison")
    args = parser.parse_args()
    setup(args.directory, int8_comparison=args.int8_comparison)
