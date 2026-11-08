import contextlib
import io
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from microphone_capture import main, pulse_environment


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
            with patch("microphone_capture.pulse_environment", side_effect=lambda env, source: env), patch("microphone_capture.shutil.which", return_value="arecord"), patch("microphone_capture.subprocess.run", side_effect=capture), contextlib.redirect_stdout(io.StringIO()):
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
            with patch("microphone_capture.pulse_environment", side_effect=lambda env, source: env), patch("microphone_capture.shutil.which", return_value="arecord"), patch("microphone_capture.subprocess.run", side_effect=capture), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--output", str(output)]), 1)
            self.assertEqual(list(Path(directory).iterdir()), [])


class PulseEnvironmentTests(unittest.TestCase):
    def test_system_service_finds_user_socket_and_real_input(self):
        result = type("Result", (), {"returncode": 0, "stdout": '[{"name":"speakers.monitor","monitor_of_sink":1},{"name":"usb-mic","monitor_of_sink":4294967295}]'})()
        with patch("microphone_capture.Path.is_socket", return_value=True), \
                patch("microphone_capture.os.getuid", return_value=1000), \
                patch("microphone_capture.shutil.which", return_value="pactl"), \
                patch("microphone_capture.subprocess.run", return_value=result):
            env = pulse_environment({})
        self.assertEqual(env["PULSE_SERVER"], "unix:/run/user/1000/pulse/native")
        self.assertEqual(env["PULSE_SOURCE"], "usb-mic")

    def test_monitor_only_and_missing_requested_input_are_rejected(self):
        result = type("Result", (), {"returncode": 0, "stdout": '[{"name":"speakers.monitor","monitor_of_sink":1}]'})()
        with patch("microphone_capture.Path.is_socket", return_value=False), \
                patch("microphone_capture.shutil.which", return_value="pactl"), \
                patch("microphone_capture.subprocess.run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "No microphone input"):
                pulse_environment({})
            result.stdout = '[{"name":"usb-mic","monitor_of_sink":4294967295}]'
            with self.assertRaisesRegex(RuntimeError, "No microphone input"):
                pulse_environment({}, "missing-mic")

    def test_explicit_server_is_preserved_and_connection_failure_is_clear(self):
        result = type("Result", (), {"returncode": 1, "stdout": ''})()
        with patch("microphone_capture.shutil.which", return_value="pactl"), \
                patch("microphone_capture.subprocess.run", return_value=result) as run:
            with self.assertRaisesRegex(RuntimeError, "Cannot connect"):
                pulse_environment({"PULSE_SERVER": "unix:/custom/audio"})
        self.assertEqual(run.call_args.kwargs["env"]["PULSE_SERVER"], "unix:/custom/audio")
