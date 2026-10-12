"""Analysis contract, signal lifecycle and audio tests; no physical hardware required."""
import hashlib
import importlib.util
import io
import json
import signal
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from animal_analysis import AnalysisCoordinator, AnalysisUnavailable
from animal_model import IDENTITY, read_labels
from animal_worker import analyze, audio_windows, ranked_candidates
from birdcast_scraper import atomic_write_json
from recording_store import RecordingStore
from reading_history import ReadingHistory
import test_dashboard


def wav_bytes(pcm=None):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(pcm if pcm is not None else b"\0\0" * 480000)
    return output.getvalue()


@unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("scipy"),
                     "Optional audio dependencies are unavailable")
class AudioTests(unittest.TestCase):
    def test_five_second_recording_produces_one_model_window(self):
        import numpy as np
        data = wav_bytes(b"\0\0" * 240000)
        self.assertEqual(audio_windows(data).shape, (1, 160000))
        session = Mock()
        session.run.return_value = [np.array([[3.2]])]
        result = analyze(data, session, ["Bubo virginianus"])
        self.assertEqual(len(result["windows"]), 1)
        self.assertEqual(result["windows"][0]["end_seconds"], 5)
        self.assertEqual(session.run.call_count, 1)
    def test_normalization_resampling_and_windows(self):
        import numpy as np
        samples = np.concatenate([np.full(240000, 16384, dtype="<i2"),
                                  np.full(240000, -16384, dtype="<i2")])
        windows = audio_windows(wav_bytes(samples.tobytes()))
        self.assertEqual(windows.shape, (2, 160000))
        self.assertEqual(windows.dtype, np.float32)
        np.testing.assert_allclose(windows[0, 100:-100], .5, atol=.0002)
        np.testing.assert_allclose(windows[1, 100:-100], -.5, atol=.0002)

    def test_resampling_rejects_wrong_frame_count(self):
        with self.assertRaises(ValueError):
            audio_windows(wav_bytes(b"\0\0" * 100))

    def test_antialias_filter_removes_out_of_band_signal(self):
        import numpy as np
        seconds = np.arange(480000) / 48000
        pcm = (16000 * np.sin(2 * np.pi * 20000 * seconds)).astype("<i2")
        samples = audio_windows(wav_bytes(pcm.tobytes()))
        self.assertLess(float(np.sqrt(np.mean(samples[:, 100:-100] ** 2))), .002)

    def test_ranking_mapping_ties_and_nonfinite_outputs(self):
        import numpy as np
        labels = ["Rana temporaria", "Bubo virginianus", "Gryllus bimaculatus",
                  "Corvus corax", "Canis lupus", "Strix varia"]
        result = ranked_candidates(np.array([3, 8, 8, -1, 0, 1]), labels)
        self.assertEqual([item["scientific_name"] for item in result],
                         ["Bubo virginianus", "Gryllus bimaculatus", "Rana temporaria", "Strix varia", "Canis lupus"])
        for bad in ([1], [float("nan")] * 6, [float("inf")] * 6):
            with self.assertRaises(ValueError):
                ranked_candidates(bad, labels)

    def test_multiple_windows_summarize_max_scores(self):
        import numpy as np
        session = Mock()
        session.run.side_effect = [[np.array([[1, 8, 3]])], [np.array([[9, 2, 4]])]]
        result = analyze(wav_bytes(), session, ["Rana temporaria", "Bubo virginianus", "Gryllus bimaculatus"])
        self.assertEqual(result["candidates"], [
            {"scientific_name": "Rana temporaria", "score": 9.0},
            {"scientific_name": "Bubo virginianus", "score": 8.0},
            {"scientific_name": "Gryllus bimaculatus", "score": 4.0}])
        self.assertEqual([(w["start_seconds"], w["end_seconds"]) for w in result["windows"]], [(0, 5), (5, 10)])
        self.assertEqual(session.run.call_args[0][1]["inputs"].shape, (1, 160000))

    def test_sound_events_are_not_reported_as_species_and_hybrid_frogs_are_kept(self):
        result = ranked_candidates([100, 2, 90, 3],
                                   ["Speech", "Bubo virginianus", "Wind", "Pelophylax 'esculentus'"])
        self.assertEqual([item["scientific_name"] for item in result],
                         ["Pelophylax 'esculentus'", "Bubo virginianus"])


class ModelTests(unittest.TestCase):
    def test_taxonomy_header_is_not_a_species(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            labels = ["Bubo virginianus"] + [f"Species {i}" for i in range(14794)]
            (path / "labels.txt").write_text("inat2024_fsd50k\n" + "\n".join(labels) + "\n")
            self.assertEqual(read_labels(path), labels)
            (path / "labels.txt").write_text("\n".join(labels))
            with self.assertRaises(ValueError):
                read_labels(path)

    def test_setup_checksum_mismatch_never_publishes_manifest(self):
        from setup_animal_model import setup
        with tempfile.TemporaryDirectory() as directory:
            with patch("setup_animal_model.urlopen", return_value=io.BytesIO(b"corrupted model")):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    setup(Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])


class QueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = RecordingStore(self.root / "recordings")
        self.store.directory.mkdir()
        self.identifier = "a" * 32
        self.store.path(self.identifier).write_bytes(wav_bytes())
        self.metadata, _ = self.store.read(self.identifier)
        self.store.publish_metadata(self.metadata)
        self.coordinator = AnalysisCoordinator(self.root, self.store)
        self.addCleanup(self.coordinator.stop)
        self.available = patch.object(self.coordinator, "availability", return_value=None)
        self.available.start()
        self.addCleanup(self.available.stop)
        self.stub = self.root / "stub.py"
        self.marker = self.root / "received"
        self.stub.write_text('''import json, sys, time
from pathlib import Path
print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    Path(sys.argv[3]).touch()
    time.sleep(float(sys.argv[1]))
    if sys.argv[2] == 'crash': sys.exit(2)
    if sys.argv[2] == 'partial':
        sys.stdout.write('{'); sys.stdout.flush(); time.sleep(5); continue
    print(json.dumps({"candidates": [{"scientific_name": "Bubo virginianus", "score": 3.2}],
                      "windows": [{"start_seconds": 0, "end_seconds": 5, "candidates": []}]}), flush=True)
''')
        self.use_stub()

    def use_stub(self, delay=0, mode="ok"):
        self.coordinator._command = lambda: [sys.executable, str(self.stub), str(delay), mode, str(self.marker)]

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("Timed out waiting for analysis state")

    def state(self):
        return self.coordinator.status(self.metadata)["state"]

    def test_completion_persistence_cached_request_and_audio_unchanged(self):
        history = ReadingHistory(self.root)
        self.coordinator.on_result = history.recording
        original = self.store.path(self.identifier).read_bytes()
        self.assertEqual(self.coordinator.request(self.metadata)["state"], "queued")
        self.wait_for(lambda: self.state() == "completed")
        self.wait_for(lambda: bool(history.recent("microphone")))
        self.assertEqual(history.recent("microphone")[0]["result"]["animals"],
                         [{"scientific_name": "Bubo virginianus", "score": 3.2}])
        pid = self.coordinator._process.pid
        completed = self.coordinator.request(self.metadata)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(self.coordinator._process.pid, pid)
        self.assertEqual(self.store.path(self.identifier).read_bytes(), original)
        restarted = AnalysisCoordinator(self.root, self.store, enabled=False)
        self.assertEqual(restarted.status(self.metadata), completed)

    def test_duplicate_pending_request_uses_one_job(self):
        self.use_stub(.15)
        first = self.coordinator.request(self.metadata)
        second = self.coordinator.request(self.metadata)
        self.assertEqual(first["queued_at"], second["queued_at"])
        self.wait_for(lambda: self.state() == "completed")
        self.assertEqual(len(list(self.coordinator.directory.glob("*.json"))), 1)

    def test_recover_interrupted_job_but_leave_old_recordings_unanalyzed(self):
        state = {"recording_id": self.identifier, "sha256": self.metadata["sha256"],
                 "model": IDENTITY, "state": "running", "queued_at": "2026-10-01"}
        atomic_write_json(self.coordinator.directory / f"{self.identifier}.json", state)
        self.coordinator.start()
        self.wait_for(lambda: self.state() == "completed")
        other = {**self.metadata, "recording_id": "b" * 32}
        self.assertEqual(self.coordinator.status(other)["state"], "not_analyzed")

    def test_model_identity_change_invalidates_results(self):
        self.coordinator.request(self.metadata)
        self.wait_for(lambda: self.state() == "completed")
        state = self.coordinator.status(self.metadata)
        state["model"]["processing_version"] = -1
        self.coordinator._write(state)
        self.assertEqual(self.coordinator.status(self.metadata), {"state": "not_analyzed", "outdated": True})

    def test_changed_audio_is_rejected_before_inference(self):
        with self.coordinator.capture():
            self.coordinator.request(self.metadata)
            self.store.path(self.identifier).write_bytes(wav_bytes(b"\1\0" * 480000))
        self.wait_for(lambda: self.state() == "failed")
        self.assertIn("changed", self.coordinator.status(self.metadata)["error"])
        self.assertFalse(self.marker.exists())

    def test_crash_and_partial_response_timeout_are_retryable(self):
        for mode in ("crash", "partial"):
            self.coordinator.timeout = .15
            self.use_stub(0, mode)
            self.coordinator.request(self.metadata)
            self.wait_for(lambda: self.state() == "failed")
            self.assertIsNone(self.coordinator._process)
        self.use_stub()
        self.coordinator.timeout = 2
        self.coordinator.request(self.metadata)
        self.wait_for(lambda: self.state() == "completed")

    def test_capture_pauses_worker_and_timeout_excludes_pause(self):
        self.coordinator.timeout = .4
        self.use_stub(.15)
        self.coordinator.request(self.metadata)
        self.wait_for(self.marker.exists)
        with self.coordinator.capture():
            self.assertIsNotNone(self.coordinator._paused_at)
            time.sleep(.5)
            self.assertEqual(self.state(), "running")
        self.wait_for(lambda: self.state() == "completed")
        self.assertLess(self.coordinator.status(self.metadata)["processing_seconds"], .4)

    def test_capture_failure_resumes_worker(self):
        self.coordinator._process = Mock()
        self.coordinator._process.poll.return_value = None
        with self.assertRaisesRegex(RuntimeError, "recorder"):
            with self.coordinator.capture():
                raise RuntimeError("recorder failed")
        self.coordinator._process.send_signal.assert_any_call(signal.SIGSTOP)
        self.coordinator._process.send_signal.assert_any_call(signal.SIGCONT)
        self.coordinator._process = None

    def test_stop_requeues_inflight_job_and_releases_process(self):
        self.use_stub(2)
        self.coordinator.request(self.metadata)
        self.wait_for(self.marker.exists)
        self.coordinator.stop()
        self.assertIsNone(self.coordinator._process)
        self.assertEqual(self.state(), "queued")

    def test_missing_assets_and_storage_failure_do_not_lose_recording(self):
        self.available.stop()
        self.assertEqual(self.coordinator.automatic(self.metadata)["state"], "unavailable")
        with self.assertRaises(AnalysisUnavailable):
            self.coordinator.request(self.metadata)
        self.available.start()
        with patch.object(self.coordinator, "_write", side_effect=OSError("disk full")):
            self.assertEqual(self.coordinator.automatic(self.metadata)["state"], "unavailable")
        self.assertEqual(hashlib.sha256(self.store.read(self.identifier)[1]).hexdigest(), self.metadata["sha256"])


class AnalysisApiTests(unittest.TestCase):
    # Reuse the existing in-memory HTTP boundary without duplicating its test cases.
    setUp = test_dashboard.DashboardTests.setUp
    request = test_dashboard.DashboardTests.request
    record = test_dashboard.DashboardTests.record
    capture = test_dashboard.DashboardTests.capture
    def test_analysis_routes_validation_and_download_contract(self):
        metadata = self.capture()
        identifier = metadata["recording_id"]
        endpoint = f"/v1/recordings/{identifier}/analysis"
        status, _, response = self.request("GET", endpoint)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(response)["error"]["code"], "analysis_unavailable")
        self.assertEqual(self.request("POST", endpoint)[0], 503)
        self.assertEqual(self.request("POST", endpoint, {"species": "frog"})[0], 400)
        self.assertEqual(self.request("POST", "/v1/recordings/nope/analysis")[0], 404)
        self.assertEqual(self.request("GET", "/v1/recordings/nope/analysis")[0], 404)
        self.assertEqual(self.request("GET", metadata["download_path"])[1]["Content-Type"], "audio/wav")
        queued = {"state": "queued", "queued_at": "2026-10-11"}
        with patch.object(self.service.animal_analysis, "request", return_value=queued):
            self.assertEqual(self.request("POST", endpoint)[0], 202)
        with patch.object(self.service.animal_analysis, "request", return_value={"state": "completed"}):
            self.assertEqual(self.request("POST", endpoint)[0], 200)
        listing = json.loads(self.request("GET", "/v1/recordings")[2])
        self.assertEqual(listing["recordings"][0]["analysis"]["state"], "unavailable")

    def test_successful_capture_automatically_queues_without_waiting(self):
        with patch("birdcast_service.subprocess.run", side_effect=self.record), \
                patch.object(self.service.animal_analysis, "automatic", return_value={"state": "queued"}) as enqueue:
            status, _, body = self.request("POST", "/v1/recordings")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["analysis"]["state"], "queued")
        enqueue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
