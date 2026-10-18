import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from wled_serial import WLEDSerial


class WLEDSerialTests(unittest.TestCase):
    def setUp(self):
        self.params = {'on': True, 'bri': 80, 'seg': [{'id': 0, 'col': [[0, 255, 68, 0]]}]}
        self.reply = {'state': {'on': True, 'bri': 80, 'seg': [{'id': 0, 'col': [[0, 255, 68, 0]]}]},
                      'info': {'brand': 'WLED'}}
        self.connection = MagicMock()
        self.connection.write.side_effect = len
        self.connection.read_until.return_value = json.dumps(self.reply).encode() + b'\n'
        self.serial = patch('wled_serial.serial.Serial', return_value=self.connection)
        self.factory = self.serial.start()
        self.addCleanup(self.serial.stop)

    def test_identifies_wled_and_reuses_connection(self):
        light = WLEDSerial(port='/dev/serial/by-id/wled')
        light.send(self.params)
        self.assertFalse(self.connection.dtr)
        self.assertFalse(self.connection.rts)
        self.assertEqual(self.connection.port, '/dev/serial/by-id/wled')
        writes = [json.loads(call.args[0]) for call in self.connection.write.call_args_list]
        self.assertEqual(writes[0], {'v': True})
        self.assertEqual(writes[1], {**self.params, 'v': True})
        light.send(self.params)
        self.factory.assert_called_once()
        light.close()
        self.connection.close.assert_called_once()

    def test_automatic_selection_requires_one_usb_serial_device(self):
        for ports in ([], [SimpleNamespace(device='a', vid=1), SimpleNamespace(device='b', vid=2)]):
            with patch('wled_serial.list_ports.comports', return_value=ports), patch.dict('os.environ', {}, clear=True):
                with self.assertRaisesRegex(OSError, 'BIOGLOW_WLED_PORT'):
                    WLEDSerial().send(self.params)
        self.factory.assert_not_called()
        with patch('wled_serial.list_ports.comports', return_value=[SimpleNamespace(device='/dev/ttyUSB0', vid=1)]), patch.dict('os.environ', {}, clear=True):
            WLEDSerial().send(self.params)
        self.assertEqual(self.connection.port, '/dev/ttyUSB0')

    def test_wrong_firmware_is_not_sent_lighting_commands(self):
        self.connection.read_until.return_value = b'{"state": {}, "info": {"brand": "Other"}}\n'
        with self.assertRaisesRegex(OSError, 'identify as WLED'):
            WLEDSerial(port='test').send(self.params)
        self.assertEqual(self.connection.write.call_count, 1)
        self.connection.close.assert_called_once()

    def test_missing_acknowledgement_closes_connection(self):
        self.connection.read_until.side_effect = [json.dumps(self.reply).encode(), OSError('unplugged')]
        light = WLEDSerial(port='test')
        with self.assertRaisesRegex(OSError, 'unplugged'):
            light.send(self.params)
        self.assertIsNone(light.connection)
        self.connection.close.assert_called_once()

    def test_mismatched_state_is_not_claimed_as_success(self):
        for key, value in (('on', False), ('bri', 12), ('seg', [])):
            wrong = json.loads(json.dumps(self.reply))
            wrong['state'][key] = value
            self.connection.read_until.side_effect = [json.dumps(self.reply).encode(), json.dumps(wrong).encode()]
            with self.assertRaisesRegex(OSError, 'acknowledge'):
                WLEDSerial(port='test').send(self.params)

    def test_boot_output_and_fragmented_reply(self):
        reply = json.dumps(self.reply).encode() + b'\n'
        self.connection.read_until.side_effect = [b'booting\n', reply[:25], reply[25:], reply]
        WLEDSerial(port='test').send(self.params)
        self.assertEqual(self.connection.write.call_count, 2)

    def test_silent_controller_times_out(self):
        light = WLEDSerial(port='test')
        light.connection = self.connection
        self.connection.read_until.return_value = b''
        with patch('wled_serial.time.monotonic', side_effect=[0, 0, 4]):
            with self.assertRaisesRegex(OSError, 'No WLED JSON response'):
                light.send(self.params)
        self.connection.close.assert_called_once()
