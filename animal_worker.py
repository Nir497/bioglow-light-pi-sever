#!/usr/bin/env python3
"""Persistent, isolated CPU inference process using a newline JSON protocol."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from animal_model import (MODEL_FILE, INT8_FILE, INT8_SHA256,
                          check_assets, checksum, is_species_label, read_labels)
from recording_store import RecordingStore


def audio_windows(data: bytes):
    import numpy as np
    from scipy.signal import resample_poly

    samples = np.frombuffer(data, dtype="<i2", offset=44).astype(np.float32) / 32768.0
    resampled = resample_poly(samples, 2, 3).astype(np.float32)
    if resampled.shape != (320000,) or not np.isfinite(resampled).all():
        raise ValueError("Invalid resampled audio.")
    return resampled.reshape(2, 160000)


def ranked_candidates(scores, labels: list[str]) -> list[dict]:
    import numpy as np

    values = np.asarray(scores)
    if values.shape != (len(labels),) or not np.isfinite(values).all():
        raise ValueError("Model returned invalid species scores.")
    species = np.array([index for index, label in enumerate(labels) if is_species_label(label)], dtype=np.int64)
    indices = species[np.argsort(-values[species], kind="stable")[:5]]
    return [{"scientific_name": labels[int(index)], "score": float(values[index])}
            for index in indices]


def analyze(data: bytes, session, labels: list[str]) -> dict:
    windows = []
    for index, samples in enumerate(audio_windows(data)):
        scores = session.run(["label"], {"inputs": samples[None, :]})[0]
        candidates = ranked_candidates(scores[0], labels)
        windows.append({"start_seconds": index * 5, "end_seconds": (index + 1) * 5,
                        "candidates": candidates})
    combined = {}
    for window in windows:
        for candidate in window["candidates"]:
            name = candidate["scientific_name"]
            previous = combined.get(name)
            if previous is None or candidate["score"] > previous["score"]:
                combined[name] = candidate.copy()
    summary = sorted(combined.values(), key=lambda item: (-item["score"], item["scientific_name"]))[:5]
    return {"candidates": summary, "windows": windows}


def emit(payload: dict) -> None:
    print(json.dumps(payload, allow_nan=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--recordings", type=Path, required=True)
    parser.add_argument("--int8", action="store_true",
                        help="Use the verified INT8 model for offline comparison only")
    args = parser.parse_args()
    os.nice(10)
    try:
        check_assets(args.assets, verify=True)
        labels = read_labels(args.assets)
        import onnxruntime as ort
        model_file = MODEL_FILE
        if args.int8:
            model_file = INT8_FILE
            reference = args.assets / model_file
            if reference.is_symlink() or checksum(reference) != INT8_SHA256:
                raise ValueError("INT8 model checksum failed.")

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        session = ort.InferenceSession(str(args.assets / model_file), sess_options=options,
                                     providers=["CPUExecutionProvider"])
        if "label" not in [output.name for output in session.get_outputs()]:
            raise ValueError("Model has no species output.")
        emit({"ready": True})
    except Exception as exc:
        emit({"error": str(exc)})
        return
    store = RecordingStore(args.recordings)
    for line in sys.stdin:
        started = time.monotonic()
        try:
            job = json.loads(line)
            metadata, data = store.read(job["recording_id"])
            if metadata["sha256"] != job["sha256"]:
                raise ValueError("Recording changed since analysis was queued.")
            result = analyze(data, session, labels)
            emit({**result, "processing_seconds": time.monotonic() - started})
        except Exception as exc:
            emit({"error": str(exc)})


if __name__ == "__main__":
    main()
