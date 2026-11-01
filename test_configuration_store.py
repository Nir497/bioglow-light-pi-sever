"""Durability, strict validation, serialization and preview conflict tests."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from configuration_store import ConfigurationStore, DEFAULTS, FORMAT, validate_backup
from birdcast_service import Service, make_handler, SUPPORTED_REGIONS


def backup(shared=None):
    return dict(format=FORMAT, version=1, exported_at='2026-11-01T12:00:00Z',
                shared=dict(shared or DEFAULTS),
                browser=dict(region='US-CA', metrics=dict(radar='presence', light='lux', birdcast='birds_in_flight'),
                             ranges=dict(radar='1h', light='1h', birdcast='1d', microphone='1d'), animal=''))


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def service(self):
        service = Service(self.root, self.root / 'config.json', animal_analysis=False)
        self.addCleanup(service.lighting.stop)
        service.lighting._background = False
        service.lighting.transport = MagicMock()
        return service

    def request(self, service, path, body):
        handler = object.__new__(make_handler(service))
        handler.path = path
        handler._body = MagicMock(return_value=body)
        handler._send = MagicMock()
        handler.do_POST()
        return handler._send.call_args.args

    def test_defaults_stale_file_ignored_and_corrupt_file_preserved(self):
        stale = self.root / '.runtime' / 'lamp_config.json'
        stale.parent.mkdir()
        stale.write_text('{"alignment":51}')
        store = ConfigurationStore(self.root)
        self.assertEqual(store.shared, DEFAULTS)
        store.path.write_text('{broken')
        store = ConfigurationStore(self.root)
        self.assertIn('File preserved', store.error)
        with self.assertRaises(OSError):
            store.save(DEFAULTS)
        self.assertEqual(store.path.read_text(), '{broken')
        store.save(DEFAULTS, replace_invalid=True)
        self.assertIsNone(ConfigurationStore(self.root).error)

    def test_failed_write_retains_accepted_and_disk_state(self):
        store = ConfigurationStore(self.root)
        store.save(DEFAULTS)
        original = store.path.read_bytes()
        revision = store.revision
        for method in ['os.replace', 'os.fsync']:
            with patch('configuration_store.' + method, side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    store.save({**DEFAULTS, 'alignment': 51})
            self.assertEqual(store.path.read_bytes(), original)
            self.assertEqual(store.shared, DEFAULTS)
            self.assertEqual(store.revision, revision)

    def test_failed_directory_sync_restores_previous_file(self):
        store = ConfigurationStore(self.root)
        store.save(DEFAULTS)
        before = store.path.read_bytes()
        with patch('configuration_store.os.fsync', side_effect=[None, OSError('directory sync failed')]):
            with self.assertRaises(OSError):
                store.save({**DEFAULTS, 'alignment': 51})
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(store.shared, DEFAULTS)

    def test_full_backup_validation(self):
        valid = backup()
        self.assertEqual(validate_backup(valid, SUPPORTED_REGIONS), valid)
        invalid = []
        for key in valid:
            value = copy.deepcopy(valid)
            del value[key]
            invalid.append(value)
        for key, item in [('version', 2), ('version', True), ('exported_at', 'yesterday'), ('extra', 1)]:
            value = copy.deepcopy(valid); value[key] = item; invalid.append(value)
        for key, item in [('alignment', True), ('alignment', 0), ('offset', 144),
                          ('beam_width', 2), ('manual_brightness', 0),
                          ('calibration_brightness', 256), ('color', 'red'), ('secret', 'x')]:
            value = copy.deepcopy(valid); value['shared'][key] = item; invalid.append(value)
        for section, key, item in [('metrics', 'radar', 'bad'), ('ranges', 'light', 'bad')]:
            value = copy.deepcopy(valid); value['browser'][section][key] = item; invalid.append(value)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                validate_backup(value, SUPPORTED_REGIONS)

    def test_normal_edits_restart_and_controller_unavailable(self):
        service = self.service()
        service.set_station_mode('direction_config')
        for spacing, width, offset in [(51, 1, 0), (49, 3, 7)]:
            status, _ = self.request(service, '/v1/direction', dict(alignment=spacing, beam_width=width, offset=offset))
            self.assertEqual(status, 200)
            restored = self.service()
            self.assertEqual((restored.direction_alignment, restored.direction_beam_width, restored.direction_offset),
                             (spacing, width, offset))
            self.assertEqual(restored.mode, 'normal')
            self.assertEqual(restored.lighting.status()['mode'], 'auto')
            self.assertFalse(restored.collection_schedule.status()['enabled'])
            restored.set_station_mode('direction_config')
            sent = restored.lighting.transport.send.call_args.args[0]['seg'][0]
            self.assertEqual((sent['grp'], sent['spc'], sent['of']), (width, spacing-width, offset))
        service.set_station_mode('manual_control')
        service.lighting.transport.send.side_effect = OSError('disconnected')
        status, result = self.request(service, '/v1/lighting', dict(brightness=91, color='#123456'))
        self.assertEqual(status, 200)
        self.assertTrue(result['saved'])
        restored = self.service()
        self.assertEqual(restored.lighting.status()['manual_brightness'], 91)
        self.assertEqual(restored.lighting.status()['color'], '#123456')

    def test_calibration_brightness_is_saved_independently(self):
        service = self.service()
        service.set_station_mode('direction_config')
        status, _ = self.request(service, '/v1/lighting', dict(mode='direction_config', brightness=83))
        self.assertEqual(status, 200)
        restored = self.service()
        self.assertEqual(restored.settings.shared['calibration_brightness'], 83)
        self.assertEqual(restored.settings.shared['manual_brightness'], 255)
        restored.set_station_mode('direction_config')
        self.assertEqual(restored.lighting.status()['alignment_brightness'], 83)

    def test_restore_conflict_validation_and_failure_change_nothing(self):
        service = self.service()
        proposed = backup({**DEFAULTS, 'alignment': 51, 'calibration_brightness': 83})
        status, preview = self.request(service, '/v1/configuration/preview', dict(backup=proposed))
        self.assertEqual(status, 200)
        self.assertEqual(service.settings.shared, DEFAULTS)
        service.settings.save({**DEFAULTS, 'color': '#112233'})
        status, _ = self.request(service, '/v1/configuration/restore', dict(backup=proposed, revision=preview['current']['revision']))
        self.assertEqual(status, 409)
        status, preview = self.request(service, '/v1/configuration/preview', dict(backup=proposed))
        before = service.settings.snapshot()
        with patch('configuration_store.os.replace', side_effect=OSError('read-only')):
            status, _ = self.request(service, '/v1/configuration/restore', dict(backup=proposed, revision=before['revision']))
        self.assertEqual(status, 500)
        self.assertEqual(service.settings.snapshot(), before)
        status, result = self.request(service, '/v1/configuration/restore', dict(backup=proposed, revision=before['revision']))
        self.assertEqual(status, 200)
        self.assertTrue(result['saved'])
        self.assertEqual(service.mode, 'normal')
        self.assertEqual(service.lighting.status()['mode'], 'auto')
        self.assertEqual(ConfigurationStore(self.root).shared, proposed['shared'])

    def test_concurrent_changes_keep_both_fields(self):
        service = self.service()
        service.set_station_mode('manual_control')
        barrier = threading.Barrier(3)
        results = []
        def change(body):
            barrier.wait()
            results.append(self.request(service, '/v1/lighting', body)[0])
        threads = [threading.Thread(target=change, args=(body,)) for body in
                   [dict(brightness=77), dict(color='#abcdef')]]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join()
        self.assertEqual(results, [200, 200])
        saved = ConfigurationStore(self.root).shared
        self.assertEqual(saved['manual_brightness'], 77)
        self.assertEqual(saved['color'], '#abcdef')


if __name__ == '__main__':
    unittest.main()
