"""Pinned Perch assets; this module has no ML dependencies."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

REVISION = "ccf5c5115178359914097dc48dc80e60d19946ed"
MODEL_FILE = "perch_v2_no_dft.onnx"
MODEL_SHA256 = "4dcf71c18a147198545944bb5149697e89e3ad2e16637fa8f0edf6d13035a017"
LABEL_SHA256 = "e4d5c0397d8fb08bf90c6b13a34810af53504faad927e472fcc567793c9de057"
INT8_FILE = "perch_v2_int8_arm.onnx"
INT8_SHA256 = "ff32ca8c57954a86e6023a915d018dc7573cfc5567dd7314899d1c947cc6d5c5"
PROCESSING_VERSION = 1
IDENTITY = {"name": "Perch 2.0 FP32 ONNX", "revision": REVISION,
            "model_sha256": MODEL_SHA256, "labels_sha256": LABEL_SHA256,
            "processing_version": PROCESSING_VERSION}


def checksum(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def check_assets(directory: Path, verify: bool = False) -> None:
    manifest = directory / "manifest.json"
    if manifest.is_symlink() or json.loads(manifest.read_text()) != IDENTITY:
        raise ValueError("Perch manifest does not match the pinned model.")
    for filename, digest in ((MODEL_FILE, MODEL_SHA256), ("labels.txt", LABEL_SHA256)):
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing Perch asset: {filename}.")
        if verify and checksum(path) != digest:
            raise ValueError(f"Perch checksum failed: {filename}.")


def read_labels(directory: Path) -> list[str]:
    lines = (directory / "labels.txt").read_text(encoding="utf-8").splitlines()
    # This export includes a taxonomy header, which is not a model output class.
    if not lines or lines[0] != "inat2024_fsd50k":
        raise ValueError("Unexpected Perch label format.")
    labels = lines[1:]
    if len(labels) != 14795 or len(set(labels)) != len(labels) or any(not x for x in labels):
        raise ValueError("Unexpected Perch label count or duplicate labels.")
    if "Bubo virginianus" not in labels:
        raise ValueError("Great horned owl is missing from the model labels.")
    return labels


def is_species_label(label: str) -> bool:
    # The other 198 labels are FSD50K sound events (Speech, Wind, etc.).
    # Quoted epithets cover the two Pelophylax frog hybrid classes in this export.
    return re.fullmatch(r"[A-Z][a-z]+ (?:[a-z][a-z-]+|'[a-z][a-z-]+')(?: [a-z][a-z-]+)?", label) is not None
