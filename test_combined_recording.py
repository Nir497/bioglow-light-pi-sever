"""Verify the production combined server without sockets or microphone hardware."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

spec = importlib.util.spec_from_file_location('combined_audio', Path(__file__).resolve().parent.parent / 'bioglow-birdcast.py')
combined = importlib.util.module_from_spec(spec)
spec.loader.exec_module(combined)


class CombinedRecordingTests(unittest.TestCase):
    def request(self, service, method, path):
        handler = object.__new__(combined.make_handler(service))
        handler.command, handler.path = method, path
        handler.connection = Mock()
        handler.headers = {}
        handler.rfile, handler.wfile = io.BytesIO(), io.BytesIO()
        statuses, headers = [], {}
        handler.send_response = statuses.append
        handler.send_header = lambda name, value: headers.update({name: value})
        handler.end_headers = lambda: None
        handler.dispatch()
        return statuses[0], headers, handler.wfile.getvalue()

    def recorder(self, rate=48000):
        def run(command, **kwargs):
            with wave.open(command[-1], 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(rate)
                audio.writeframes(b'\x01\x00' * combined.FRAMES)
            return subprocess.CompletedProcess(command, 0, '', '')
        return run

    def test_create_download_format_and_integrity_with_light_disabled(self):
        with tempfile.TemporaryDirectory(dir=combined.HOME) as directory:
            service = combined.Service(Path(directory))
            self.assertFalse(hasattr(service, 'light_sensor'))
            with patch.object(combined.subprocess, 'run', side_effect=self.recorder()):
                status, _, body = self.request(service, 'POST', '/v1/recordings')
            self.assertEqual(status, 201)
            metadata = json.loads(body)
            status, headers, data = self.request(service, 'GET', metadata['download_url'])
            self.assertEqual(status, 200)
            self.assertEqual(headers['Content-Type'], 'audio/wav')
            self.assertEqual(int(headers['Content-Length']), 960044)
            self.assertEqual(len(data), metadata['byte_length'])
            self.assertEqual(hashlib.sha256(data).hexdigest(), metadata['sha256'])
            self.assertEqual(headers['X-Content-SHA256'], metadata['sha256'])
            with wave.open(io.BytesIO(data), 'rb') as audio:
                self.assertEqual((audio.getnchannels(), audio.getframerate(), audio.getsampwidth(), audio.getnframes(), audio.getcomptype()), (1, 48000, 2, 480000, 'NONE'))

    def test_wrong_sample_rate_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=combined.HOME) as directory:
            service = combined.Service(Path(directory))
            with patch.object(combined.subprocess, 'run', side_effect=self.recorder(rate=44100)):
                status, _, body = self.request(service, 'POST', '/v1/recordings')
            self.assertEqual(status, 422)
            self.assertEqual(json.loads(body)['error']['code'], 'invalid_recording')


if __name__ == '__main__':
    unittest.main()
