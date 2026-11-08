import unittest
from unittest.mock import Mock, patch

from birdcast_service import Service
from lighting_control import LightingControl
from sound_classification import classify_sound


class SoundTests(unittest.TestCase):
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
            {'recording_id': 'new'}, {'recording_id': 'old'}]
        service.animal_analysis.request.return_value = {'state': 'queued'}
        self.assertEqual(service.latest_sound()['source'], 'unknown')
        service.animal_analysis.request.assert_called_once_with({'recording_id': 'new'}, sound=True)
        service.latest_sound()
        self.assertEqual(service.animal_analysis.request.call_count, 1)
        service._sound_checked_at = float('-inf')
        service.animal_analysis.request.return_value = {'state': 'completed', 'sound': {'source': 'human'}}
        self.assertEqual(service.latest_sound()['source'], 'human')

    def test_sound_gate_immediately_extinguishes_auto_but_preserves_manual(self):
        transport = Mock()
        transport.send.return_value = {}
        light = LightingControl(transport=transport, clock=lambda: 0)
        self.addCleanup(light.stop)
        light.sound_reader = Mock()
        with patch.object(light, '_auto_allowed', return_value=True), patch.object(light, '_radar_age', return_value=0):
            light._sound = {'source': 'human'}
            light._evaluate_adaptive(None, 100)
            self.assertGreater(light.status()['target_brightness'], 0)
            for source in ('animal', 'unknown'):
                light._sound = {'source': source}
                light._evaluate_adaptive(None, 100)
                self.assertEqual(light.status()['target_brightness'], 0)
            light.set_mode('on')
            self.assertEqual(light.status()['target_brightness'], 255)
