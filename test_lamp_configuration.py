"""Verify durable preferences independently of hardware and sensor state."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lamp_configuration import ConfigurationSaveError, LampConfiguration


class LampConfigurationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = LampConfiguration(self.root)

    def test_missing_file_and_round_trip(self):
        self.assertEqual(self.store.status()["persistence_status"], "defaults")
        self.assertFalse(self.store.path.exists())
        self.store.save({"alignment": 12, "beam_width": 5, "offset": 8,
                         "manual_brightness": 71, "color": "#ABCDEF"})
        restored = LampConfiguration(self.root)
        self.assertEqual(restored.settings, {"alignment": 12, "beam_width": 5,
                         "offset": 8, "manual_brightness": 71, "color": "#abcdef"})
        self.assertEqual(restored.status()["persistence_status"], "saved")
        snapshot = restored.status()
        snapshot["settings"]["color"] = "changed"
        self.assertEqual(restored.settings["color"], "#abcdef")

    def test_invalid_files_use_defaults_and_remain_untouched(self):
        self.store.path.parent.mkdir()
        invalid = ['{broken', '[]', '{"version":2}', '{"version":true}',
                   '{"version":1}', json.dumps({"version": 1, "settings": {}})]
        for raw in invalid:
            with self.subTest(raw=raw):
                self.store.path.write_text(raw)
                with self.assertLogs("lamp_configuration", level="WARNING"):
                    restored = LampConfiguration(self.root)
                self.assertEqual(restored.settings, LampConfiguration.DEFAULTS)
                self.assertEqual(restored.status()["persistence_status"], "error")
                self.assertEqual(self.store.path.read_text(), raw)
                restored.save({"color": "#123456"})
                self.assertEqual(LampConfiguration(self.root).settings["color"], "#123456")

    def test_validation_does_not_change_saved_file(self):
        self.store.save({})
        original = self.store.path.read_bytes()
        for change in ({"alignment": True}, {"alignment": 0}, {"beam_width": 2},
                       {"alignment": 1, "beam_width": 3}, {"offset": 144},
                       {"manual_brightness": False}, {"manual_brightness": 256},
                       {"color": "bad"}, {"extra": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.save(change)
            self.assertEqual(self.store.path.read_bytes(), original)

    def test_interrupted_replace_preserves_file_and_memory(self):
        self.store.save({"color": "#123456"})
        original = self.store.path.read_bytes()
        with patch("birdcast_scraper.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(ConfigurationSaveError):
                self.store.save({"color": "#abcdef"})
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.settings["color"], "#123456")
        self.assertEqual(list(self.store.path.parent.glob("*.tmp")), [])
        self.assertEqual(self.store.status()["persistence_status"], "error")
        self.store.save({"color": "#abcdef"})
        self.assertIsNone(self.store.error)

    def test_unchanged_saved_preferences_do_not_write(self):
        self.store.save({})
        with patch("lamp_configuration.atomic_write_json") as write:
            self.store.save({"color": "#FFA028"})
            write.assert_not_called()
