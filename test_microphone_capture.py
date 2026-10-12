import contextlib
import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from microphone_capture import main


class MicrophoneCaptureTests(unittest.TestCase):
    def test_exact_five_second_pcm_is_published_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/"recording.wav"
            def capture(command, **kwargs):
                self.assertEqual(command[command.index("-d")+1], "5")
                with wave.open(command[-1], "wb") as audio:
                    audio.setnchannels(1)
                    audio.setframerate(48000)
                    audio.setsampwidth(2)
                    audio.writeframes(b"\0\0"*240000)
            with patch("microphone_capture.shutil.which", return_value="arecord"), patch("microphone_capture.subprocess.run", side_effect=capture), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--output", str(output)]), 0)
            self.assertEqual(output.stat().st_size, 480044)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_incomplete_capture_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/"recording.wav"
            def capture(command, **kwargs):
                with wave.open(command[-1], "wb") as audio:
                    audio.setnchannels(1)
                    audio.setframerate(48000)
                    audio.setsampwidth(2)
                    audio.writeframes(b"\0\0"*100)
            with patch("microphone_capture.shutil.which", return_value="arecord"), patch("microphone_capture.subprocess.run", side_effect=capture), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--output", str(output)]), 1)
            self.assertEqual(list(Path(directory).iterdir()), [])
