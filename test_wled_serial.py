import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from wled_serial import WLEDSerial, SerialReplyTimeout


class WLEDSerialTests(unittest.TestCase):
    def setUp(self):
        self.params = {'on': True, 'bri': 80, 'seg': [{'id': 0, 'col': [[0, 255, 68, 0]]}]}
        self.reply = {'state': {'on': True, 'bri': 80, 'seg': [{'id': 0, 'start': 0, 'stop': 192, 'grp': 1, 'spc': 0, 'on': True, 'bri': 255, 'fx': 0, 'pal': 0, 'frz': False, 'of': 0, 'col': [[0, 255, 68, 0]]}]},
                      'info': {'brand': 'WLED', 'arch': 'esp32', 'ver': '0.14.4', 'leds': {'count': 192}}}
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
        self.assertEqual(writes[1]['on'], True)
        self.assertEqual(writes[1]['bri'], 80)
        self.assertEqual(writes[1]['seg'][0]['start'], 0)
        self.assertEqual(writes[1]['seg'][0]['stop'], 192)
        self.assertEqual(writes[1]['seg'][0]['spc'], 0)
        self.assertTrue(writes[1]['v'])
        light.send(self.params)
        self.factory.assert_called_once()
        light.close()
        self.connection.close.assert_called_once()

    def test_full_strip_replaces_spotlight_and_disables_extra_segments(self):
        current = json.loads(json.dumps(self.reply))
        current['state']['seg'] = [{'id': 0, 'start': 50, 'stop': 70, 'spc': 2},
                                   {'id': 2, 'start': 70, 'stop': 100}]
        self.connection.read_until.side_effect = [json.dumps(current).encode(), json.dumps(self.reply).encode()]
        result = WLEDSerial(port='test').send(self.params)
        command = json.loads(self.connection.write.call_args.args[0])
        self.assertEqual(command['seg'][0]['start'], 0)
        self.assertEqual(command['seg'][0]['stop'], 192)
        self.assertEqual(command['seg'][0]['grp'], 1)
        self.assertEqual(command['seg'][0]['spc'], 0)
        self.assertEqual(command['seg'][1], {'id': 2, 'stop': 0})
        self.assertEqual(command['mainseg'], 0)
        self.assertEqual(command['pl'], -1)
        self.assertFalse(command['nl']['on'])
        self.assertEqual(result['covered_leds'], 192)
        self.assertEqual(result['led_count'], 192)

    def test_wled_color_and_full_brightness_payloads(self):
        from lighting_control import LightingControl
        for color, rgb in (('#ff0000', [255, 0, 0]), ('#00ff00', [0, 255, 0]),
                           ('#0000ff', [0, 0, 255]), ('#ffffff', [255, 255, 255]),
                           ('#000000', [0, 0, 0]), ('#1280ef', [18, 128, 239])):
            for brightness in (1, 128, 255):
                with self.subTest(color=color, brightness=brightness):
                    ack = json.loads(json.dumps(self.reply))
                    ack['state']['bri'] = brightness
                    ack['state']['seg'][0]['col'] = [rgb + [0]]
                    ack['info']['leds'].update(maxpwr=3000, pwr=1800)
                    self.connection.read_until.side_effect = [json.dumps(self.reply).encode(), json.dumps(ack).encode()]
                    light = LightingControl(transport=WLEDSerial(port='test'))
                    result = light.configure(mode='on', color=color, brightness=brightness)
                    self.assertEqual(result['status'], 'ok')
                    command = json.loads(self.connection.write.call_args.args[0])
                    self.assertEqual(command['bri'], brightness)
                    self.assertEqual(command['seg'][0]['bri'], 255)
                    self.assertEqual(command['seg'][0]['col'], [rgb + [0]])
                    self.assertEqual(command['seg'][0]['fx'], 0)
                    self.assertEqual(command['seg'][0]['pal'], 0)
                    self.assertEqual(command['seg'][0]['stop'], 192)
                    device = light.status()['device']
                    self.assertEqual(device['confirmed_color'], color)
                    self.assertEqual(device['confirmed_brightness'], brightness)
                    self.assertEqual(device['current_limit_ma'], 3000)
                    self.assertEqual(device['estimated_current_ma'], 1800)
                    # Brightness commands must not alter the hardware power budget.
                    self.assertNotIn('hw', command)
                    light.stop()
        self.connection.read_until.side_effect = None

    def test_unrequested_white_channel_is_not_accepted_as_the_selected_color(self):
        wrong = json.loads(json.dumps(self.reply))
        wrong['state']['seg'][0]['col'][0][3] = 255
        self.connection.read_until.side_effect = [json.dumps(self.reply).encode(), json.dumps(wrong).encode()]
        with self.assertRaisesRegex(OSError, 'RGBW color'):
            WLEDSerial(port='test').send(self.params)

    def test_configured_output_shorter_than_192_is_not_success(self):
        self.reply['info']['leds']['count'] = 100
        self.reply['state']['seg'][0]['stop'] = 100
        self.connection.read_until.return_value = json.dumps(self.reply).encode()
        light = WLEDSerial(port='test')
        with self.assertRaisesRegex(OSError, '100 pixels, but your strip has 192'):
            light.send(self.params)
        self.assertEqual(self.connection.write.call_count, 1)
        self.assertEqual(light.device_info['led_count'], 100)
        self.assertNotIn('covered_leds', light.device_info)

    def test_off_still_works_with_a_short_configured_output(self):
        current = json.loads(json.dumps(self.reply))
        current['info']['leds']['count'] = 100
        current['state']['seg'][0]['stop'] = 100
        off = json.loads(json.dumps(current))
        off['state']['on'] = False
        self.connection.read_until.side_effect = [json.dumps(current).encode(), json.dumps(off).encode()]
        result = WLEDSerial(port='test').send({**self.params, 'on': False, 'bri': 0})
        self.assertEqual(result['covered_leds'], 100)
        self.assertFalse(result['full_strip_confirmed'])

    def test_larger_configured_bus_targets_the_known_192_pixel_strip(self):
        self.reply['info']['leds']['count'] = 300
        self.connection.read_until.return_value = json.dumps(self.reply).encode()
        result = WLEDSerial(port='test').send(self.params)
        self.assertEqual(result['covered_leds'], 192)
        command = json.loads(self.connection.write.call_args.args[0])
        self.assertEqual(command['seg'][0]['stop'], 192)

    def test_missing_count_does_not_guess_from_length(self):
        del self.reply['info']['leds']
        self.connection.read_until.return_value = json.dumps(self.reply).encode()
        with self.assertRaisesRegex(OSError, 'LED count'):
            WLEDSerial(port='test').send(self.params)
        self.assertEqual(self.connection.write.call_count, 1)

    def test_partial_strip_acknowledgement_is_failure(self):
        for key, value in (('start', 10), ('stop', 100), ('spc', 2), ('on', False), ('fx', 5), ('pal', 10), ('bri', 50), ('frz', True), ('of', 20)):
            wrong = json.loads(json.dumps(self.reply))
            wrong['state']['seg'][0][key] = value
            self.connection.read_until.side_effect = [json.dumps(self.reply).encode(), json.dumps(wrong).encode()]
            with self.assertRaisesRegex(OSError, 'full-strip coverage'):
                WLEDSerial(port='test').send(self.params)

    def test_power_off_uses_same_full_strip_command(self):
        off = json.loads(json.dumps(self.reply))
        off['state']['on'] = False
        self.connection.read_until.side_effect = [json.dumps(self.reply).encode(), json.dumps(off).encode()]
        result = WLEDSerial(port='test').send({**self.params, 'on': False, 'bri': 0})
        self.assertEqual(result['covered_leds'], 192)
        command = json.loads(self.connection.write.call_args.args[0])
        self.assertFalse(command['on'])
        self.assertEqual(command['seg'][0]['stop'], 192)

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

    def test_startup_retries_without_reopening_or_resetting_the_port(self):
        light = WLEDSerial(port='test')
        with patch.object(light, '_read_reply', side_effect=[SerialReplyTimeout('booting'), self.reply, self.reply]):
            light.send(self.params)
        self.factory.assert_called_once()
        self.connection.open.assert_called_once()
        self.connection.close.assert_not_called()
        writes = [json.loads(call.args[0]) for call in self.connection.write.call_args_list]
        self.assertEqual(writes[:2], [{'v': True}, {'v': True}])
        self.assertIn('seg', writes[2])

    def test_silent_startup_never_sends_lighting_commands(self):
        light = WLEDSerial(port='test')
        with patch.object(light, '_read_reply', side_effect=SerialReplyTimeout('no data')):
            with self.assertRaises(SerialReplyTimeout):
                light.send(self.params)
        writes = [json.loads(call.args[0]) for call in self.connection.write.call_args_list]
        self.assertEqual(writes, [{'v': True}, {'v': True}])
        self.connection.open.assert_called_once()
        self.connection.close.assert_called_once()

    def test_json_after_boot_prefix_without_newline(self):
        reply = json.dumps(self.reply).encode() + b'\n'
        self.connection.read_until.side_effect = [b'\x00ESP32 boot log: ' + reply, reply]
        WLEDSerial(port='test').send(self.params)

    def test_no_response_reports_selected_port_baud_and_received_bytes(self):
        light = WLEDSerial(port='/dev/serial/by-id/sp803e')
        light.connection = self.connection
        self.connection.read_until.return_value = b''
        with patch('wled_serial.time.monotonic', side_effect=[0, 0, 4]):
            with self.assertRaisesRegex(SerialReplyTimeout, '/dev/serial/by-id/sp803e at 115200 baud.*0 bytes received'):
                light._read_reply()

    def test_esphome_output_is_distinguished_from_silence(self):
        light = WLEDSerial(port='test')
        light.connection = self.connection
        self.connection.read_until.return_value = b'[I][app:001]: ESPHome version 2026\n'
        with patch('wled_serial.time.monotonic', side_effect=[0, 0, 4]):
            with self.assertRaisesRegex(SerialReplyTimeout, 'ESPHome output'):
                light._read_reply()

    def test_silent_controller_times_out(self):
        light = WLEDSerial(port='test')
        light.connection = self.connection
        self.connection.read_until.return_value = b''
        with patch('wled_serial.time.monotonic', side_effect=[0, 0, 4]):
            with self.assertRaisesRegex(OSError, 'No WLED JSON response'):
                light.send(self.params)
        self.connection.close.assert_called_once()
