from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch

from birdcast_service import DEFAULT_RECORDER, Service
from lighting_control import LightingControl
from sound_classification import classify_sound


class SoundTests(unittest.TestCase):
    def test_expired_or_invalid_audio_cannot_hold_normal_mode_off(self):
        service = Service.__new__(Service)
        service.recording_store = Mock()
        service.animal_analysis = Mock()
        for timestamp in ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                          (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                          'invalid', None, datetime.now().isoformat()):
            with self.subTest(timestamp=timestamp):
                service._sound_checked_at = float('-inf')
                service._sound_recordings_stamp = None
                service.recording_store.directory.stat.return_value.st_mtime_ns = None
                service._sound_recordings = [{'recording_id': 'old', 'created_at': timestamp}]
                self.assertEqual(service.latest_sound()['source'], 'unavailable')
                service.animal_analysis.request.assert_not_called()

    def test_disconnected_microphone_bypasses_old_audio_and_reconnects(self):
        service = Service.__new__(Service)
        service.recorder = DEFAULT_RECORDER
        service._sound_checked_at = float('-inf')
        service.recording_store = Mock()
        service.animal_analysis = Mock()
        service.recording_store.list_recordings.return_value = [{'recording_id': 'old', "created_at": datetime.now(timezone.utc).isoformat()}]
        service.animal_analysis.status.return_value = {'state': 'completed'}
        service.animal_analysis.request.return_value = {'state': 'completed', 'sound': {'source': 'human'}}
        with patch('birdcast_service.usb_capture_device', return_value=None) as usb, \
                patch('birdcast_service.pulse_environment', side_effect=RuntimeError('No microphone input')):
            self.assertEqual(service.latest_sound()['source'], 'unavailable')
            self.assertEqual(service.latest_sound()['source'], 'unavailable')
            self.assertEqual(usb.call_count, 1)
            service.recording_store.list_recordings.assert_not_called()
        service._microphone_checked_at = float('-inf')
        with patch('birdcast_service.usb_capture_device', return_value='plughw:CARD=Device,DEV=0'):
            self.assertEqual(service.latest_sound()['source'], 'human')

    def test_human_animal_environment_and_ambiguous_audio(self):
        labels = ['Speech', 'Bubo virginianus', 'Wind']
        for scores, expected in (([5, 1, 0], 'human'), ([0, 5, 1], 'animal'),
                                 ([0, 1, 5], 'unknown'), ([4, 4, 0], 'unknown'),
                                 ([-1, -3, -4], 'unknown')):
            self.assertEqual(classify_sound(scores, labels)['source'], expected)

    def test_latest_recording_is_requested_and_pending_is_unknown(self):
        service = Service.__new__(Service)
        service._sound_checked_at = float('-inf')
        service.recording_store = Mock()
        service.animal_analysis = Mock()
        service.recording_store.list_recordings.return_value = [
            {'recording_id': 'new', "created_at": datetime.now(timezone.utc).isoformat()}, {'recording_id': 'old', "created_at": datetime.now(timezone.utc).isoformat()}]
        service.animal_analysis.request.return_value = {'state': 'queued'}
        self.assertEqual(service.latest_sound()['source'], 'unknown')
        service.animal_analysis.request.assert_called_once_with(service.recording_store.list_recordings.return_value[0], sound=True)
        service.latest_sound()
        self.assertEqual(service.animal_analysis.request.call_count, 1)
        service._sound_checked_at = float('-inf')
        service.animal_analysis.request.return_value = {'state': 'completed', 'sound': {'source': 'human'}}
        self.assertEqual(service.latest_sound()['source'], 'human')

    def test_failed_microphone_bypasses_old_animal_recording_and_recovers(self):
        service = Service.__new__(Service)
        service._latest = {"microphone": {"http_status": 502}}
        service._sound_checked_at = float('-inf')
        service.recording_store = Mock()
        service.animal_analysis = Mock()
        service.recording_store.list_recordings.return_value = [{'recording_id': 'old', "created_at": datetime.now(timezone.utc).isoformat()}]
        service.animal_analysis.status.return_value = {'state': 'completed'}
        service.animal_analysis.request.return_value = {'state': 'completed', 'sound': {'source': 'animal'}}
        self.assertEqual(service.latest_sound()['source'], 'unavailable')
        service.animal_analysis.request.assert_not_called()
        service._latest['microphone'] = {'http_status': 200}
        self.assertEqual(service.latest_sound()['source'], 'animal')

    def test_missing_recordings_and_analysis_failure_allow_fallback(self):
        service = Service.__new__(Service)
        service._sound_checked_at = float('-inf')
        service.recording_store = Mock()
        service.animal_analysis = Mock()
        service.recording_store.list_recordings.return_value = []
        self.assertEqual(service.latest_sound()['source'], 'unavailable')
        service._sound_checked_at = float('-inf')
        service._sound_recordings = [{'recording_id': 'new', "created_at": datetime.now(timezone.utc).isoformat()}]
        service.animal_analysis.status.return_value = {'state': 'failed'}
        self.assertEqual(service.latest_sound()['source'], 'unavailable')
        service.animal_analysis.request.assert_not_called()

    def test_sound_gate_immediately_extinguishes_auto_but_preserves_manual(self):
        transport = Mock()
        transport.send.return_value = {}
        light = LightingControl(transport=transport, clock=lambda: 0)
        self.addCleanup(light.stop)
        light.sound_reader = Mock()
        with patch.object(light, '_auto_allowed', return_value=True), patch.object(light, '_radar_age', return_value=0):
            for source in ('human', 'unavailable'):
                light._sound = {'source': source}
                light._evaluate_adaptive(None, 100)
                self.assertGreater(light.status()['target_brightness'], 0)
            for source in ('animal', 'unknown'):
                light._sound = {'source': source}
                light._evaluate_adaptive(None, 100)
                self.assertEqual(light.status()['target_brightness'], 0)
            light._sound = {'source': 'unavailable'}
            light._evaluate_adaptive('Radar unavailable', 100)
            self.assertEqual(light.status()['target_brightness'], 0)
            light.set_mode('on')
            self.assertEqual(light.status()['target_brightness'], 255)
