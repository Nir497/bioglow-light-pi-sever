# Local animal identification

New recordings are automatically queued for Perch 2.0 analysis after their WAV
and integrity metadata are saved. The capture response does not wait for inference.
Existing recordings have an **Identify animals** button; failed jobs have **Retry
identification**. There is no automatic scan of old recordings.

Each recording shows up to five unverified species candidates, raw model scores, and a
control to inspect five-second analysis windows (one for new five-second captures,
two for retained ten-second recordings). The global model is used without
geographic filtering. Scientific names come from the pinned model labels and
appear alongside English common names from iNaturalist. Previously encountered
names are bundled in `animal_common_names.json`; other names use exact-name or
exact-synonym lookups cached in `.runtime/animal_common_names.json`. Scientific
names remain available if no common name is found or the station is offline.
Source: [iNaturalist API](https://api.inaturalist.org/v1/docs/).

These are suggestions, not confirmed observations. Scores are uncalibrated logits,
not percentages, and cannot tell one frog from ten frogs. Noise and silence can
produce ranked candidates. A missing suggestion does not establish absence;
overlapping animals, distant calls and unsupported species can be missed. There
is no calibrated detection threshold or population estimate. Scores have no fixed
minimum or maximum, can be negative, and are not out of 10 or 100. Their scale
differs between species. See [Google's Perch model card](https://www.kaggle.com/models/google/bird-vocalization-classifier).
The Recordings page plots raw scores over recording time for the selected animal
using `readings/recordings.txt`; only new recording events and new analyses are
logged, without importing old results. Manually review representative recordings
before relying on detections.

## Setup and operation

From `/home/nir/bioglow-birdcast`:

```bash
.venv/bin/python -m pip install -r requirements-animal.txt
.venv/bin/python setup_animal_model.py
.venv/bin/python birdcast_service.py --port 8000
```

Stop the previous combined server in its owning terminal/service before starting
the repository server. Run only one station entry point. This feature does not
edit the external server/recorder, change boot configuration, or install a system
service. See [DASHBOARD.md](DASHBOARD.md) and [presence/README.md](presence/README.md)
for station and UART setup.

The Python dependencies are optional, separately pinned and tested on 64-bit
Debian 13 / Python 3.13. Missing assets or dependencies leave recording and
downloads available and show identification as unavailable. Restart the server
after setup to resume persisted jobs. To disable new analysis:

```bash
.venv/bin/python birdcast_service.py --port 8000 --no-animal-analysis
```

Completed results remain viewable when analysis is disabled. All audio analysis
is local; recordings are never sent to a cloud API. Setup downloads only model
files. `.models/` contains model assets and `.runtime/animal_analysis/` contains
durable job states/results; both are ignored by Git. The original five- or ten-second,
mono, 48 kHz, 16-bit WAV and recording sidecar stay intact. Only an in-memory copy
is resampled to 32 kHz, with an anti-alias filter.

A persistent subprocess loads the model once, uses one inference thread and
`nice +10`, and processes jobs oldest first. OpenBLAS/OpenMP thread counts are
also limited to one and ONNX thread spinning is disabled. The coordinator pauses
this subprocess with SIGSTOP during microphone capture and resumes it with
SIGCONT even if recording fails. HTTP and sensor workers stay in the server
process. One inference job runs at a time; pending jobs are read from disk.

Loading and each job have a 120-second active-time limit; capture pauses do not
consume that limit. Worker crashes/timeouts are shown as failures and can be
retried. Shutdown preserves unfinished jobs, which resume on restart. A changed
source checksum or processing/model identity makes a completed result outdated.
Corrupt audio remains subject to the existing recording-store integrity checks.
Analysis storage errors are reported separately from successful audio capture.

## API

- `POST /v1/recordings` still returns HTTP 200 with saved recording metadata; an
  additive `analysis` field reports the initial identification state.
- `GET /v1/recordings` includes a compact `analysis` summary per recording:
  state, candidates, duration, error and/or outdated flag where applicable.
- `POST /v1/recordings/<id>/analysis` accepts an empty body or `{}`. It returns
  HTTP 202 for queued/running jobs, HTTP 200 for a valid cached result, HTTP 503
  when analysis cannot be requested, and HTTP 404 for missing recordings.
  Repeated requests reuse the same pending job; failed/outdated jobs can be retried.
- `GET /v1/recordings/<id>/analysis` returns HTTP 200 for status/results, HTTP 503
  for unavailable analysis, and HTTP 404 for missing recordings. Both successful
  analysis endpoints return `{status, recording_id, analysis}`. The GET 503 also
  includes the analysis state so the UI can explain unavailability.
- WAV downloads and legacy cleanup acknowledgments keep their existing behavior.

States are `not_analyzed`, `queued`, `running`, `completed`, `failed` and
`unavailable`. Completed results include recording SHA-256, pinned model identity,
processing version, timestamps, processing duration, `candidates` and `windows`.
Candidates contain `scientific_name` and `score`; windows contain `start_seconds`,
`end_seconds` and their own candidate lists. Summary scores are the maximum
among the retained top-five window candidates, not an average or probability.

The dashboard polls pending results every three seconds while their cards are
visible. Polling updates only the identification panel, preserving audio playback
and focus. It stops for terminal states and hidden recording views.

## Model provenance

Google developed Perch 2.0. This implementation uses the full-precision ONNX
conversion from [tphakala/Perch-v2](https://huggingface.co/tphakala/Perch-v2), at
revision `ccf5c5115178359914097dc48dc80e60d19946ed`. See also the
[Google model card](https://www.kaggle.com/models/google/bird-vocalization-classifier/tensorFlow2/perch_v2)
and [ONNX conversion project](https://huggingface.co/justinchuby/Perch-onnx).
The model is distributed under [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0); model copyright belongs to
Google, and conversion attribution belongs to the linked authors. The model
weights are downloaded separately and are not redistributed in this repository.

Setup pins SHA-256 checksums for the 413 MB full-precision model and matching labels. The
worker rechecks them before loading. The label export has one taxonomy header
followed by 14,795 classes; the header is excluded from score indexing. The 198
general sound-event classes (such as Speech and Wind) are excluded from species
suggestions, leaving 14,597 supported species classes. Setup
verifies great horned owl is supported; frogs and crickets have species-specific
classes, but not every local species is necessarily covered.

## Validation and performance

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_animal_analysis test_dashboard_frontend -v
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmark_animal_analysis.py --output .runtime/animal_benchmark.json
```

The benchmark starts a temporary server bound only to localhost on an ephemeral
port, analyzes synthetic silence and simulates radar updates. It does not open
UART or the microphone. To benchmark a retained recording locally, add
`--recording-id <32-character-id>`. Reports contain performance data only.

Measured on this Pi 4 Model B Rev 1.5 on 2026-10-11, with one low-priority inference
thread and three warm ten-second clips:

| Measurement | Result |
| --- | --- |
| First job including worker startup/model load | 12.76 seconds |
| Warm job median | 4.20 seconds |
| Sampled worker peak RSS | 847 MiB |
| Local `/v1/latest` response p95 during analysis | 5.3 ms |
| Stale synthetic radar samples during analysis | 0 |

The baseline HTTP p95 was 37.5 ms on this short sample; the lower loaded figure
does not establish that inference improves server performance. Measurements use
synthetic silence and radar frames, not physical sensor validation or wildlife
accuracy evaluation. They met the targets of warm processing under ten seconds,
HTTP p95 under 500 ms and no additional stale synthetic radar samples.

The smaller ARM INT8 model was evaluated first. Its leading species and top-five
sets differed from full precision in both windows of the retained recording
tested here. The production model therefore uses full precision; compression
agreement claims from other fixtures are not assumed to generalize to this
microphone. Neither model's species predictions have been human-verified here.

For an optional INT8 versus full-precision ranking comparison:

```bash
.venv/bin/python setup_animal_model.py --int8-comparison
.venv/bin/python compare_animal_models.py <recording-id> --output .runtime/model_comparison.json
```

This downloads an additional 131 MB pinned INT8 model. The comparison runs
the models sequentially in separate low-priority processes and stores results
only locally. Ranking agreement is not verification of animal presence. INT8
scores are not used by the production server.

The unit and frontend tests cover audio normalization/anti-aliasing/windows,
score mapping and nonfinite outputs, checksum failures, job deduplication,
restart recovery, worker crashes, partial-response timeouts, pause/resume,
analysis routing, polling cleanup and escaped labels. The frontend tests use a
minimal DOM double; actual browser playback/focus and desktop/mobile appearance
still require a browser check. Real frog, cricket, owl, overlapping-call and
background-noise clips need human review, and physical microphone/UART operation
and LAN access remain host checks. Do not infer physical radar absence from
unavailable sensor data.


Quiet recordings are retained but skipped before animal inference is queued.
The gate measures DC-corrected RMS in 100 ms blocks of validated PCM audio,
without importing model dependencies. If every block is below −50 dBFS,
the dashboard shows “Skipped: audio too quiet”. This conservative initial
threshold is relative to digital full scale, not calibrated sound pressure;
microphone noise may require later tuning. Brief sounds above the threshold
remain eligible. Previously queued recordings are also checked before the
worker starts; existing completed predictions are preserved.

Automatic lighting now checks the latest retained recording when radar reports a
moving or stationary target. Human sound candidates allow the existing ambient,
distance, motion, and migration brightness rules. Animal sounds, ambiguous/mixed
sounds, quiet audio, missing recordings, pending analysis, and analysis failures
keep automatic lighting off. Manual control and calibration remain independent.
The server checks results every two seconds; it queues older results again when
they lack sound classification. It does not start microphone recording: use the
existing recording scheduler to keep the latest recording current. There is no
age cutoff for the latest recording.

The pinned Perch model's full event output now supplies the sound decision as
well as the existing animal species candidates. A human or animal event must be
the highest scoring label, have a positive raw logit, and lead competing source
categories by at least 1 logit. These are conservative heuristics, not calibrated
probabilities. Each five-second window must agree for retained ten-second audio.
Audio cannot establish whether the radar target produced the sound; silent
people, playback, overlapping sounds, and stale recordings can be misclassified.
`lighting.sound_gate` in `/v1/latest` reports the selected recording and decision.
