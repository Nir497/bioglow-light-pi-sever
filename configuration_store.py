"""Strict portable preferences and atomic, durable storage (no runtime state)."""
import copy
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

FORMAT = 'bioglow-configuration'
DEFAULTS = dict(alignment=144, beam_width=1, offset=0, manual_brightness=255,
                calibration_brightness=64, color='#ffa028')
METRICS = {
    'radar': ['presence', 'presence_state', 'range_estimate_cm', 'detection_distance_cm',
              'moving_distance_cm', 'stationary_distance_cm', 'moving_energy_score',
              'stationary_energy_score', 'activity_level', 'presence_confidence'],
    'light': ['lux', 'ambient_counts', 'white_counts'],
    'birdcast': ['birds_in_flight', 'total_birds_crossed', 'speed_mph', 'altitude_ft'],
}


def fields(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('Missing or unknown configuration fields.')


def validate_shared(value):
    fields(value, DEFAULTS)
    for key, low, high in [('alignment', 1, 144), ('beam_width', 1, 143),
                           ('offset', 0, 143), ('manual_brightness', 1, 255),
                           ('calibration_brightness', 1, 255)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f'{key} must be an integer from {low} to {high}.')
    if value['beam_width'] % 2 != 1 or value['beam_width'] > min(143, 2 * (value['alignment'] // 2) + 1):
        raise ValueError('Invalid beam width for spacing.')
    if value['offset'] >= value['alignment']:
        raise ValueError('Offset must be smaller than spacing.')
    if not isinstance(value['color'], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', value['color']):
        raise ValueError('Color must be #RRGGBB.')
    return {**value, 'color': value['color'].lower()}


def validate_browser(value, regions):
    fields(value, ['region', 'metrics', 'ranges', 'animal'])
    if not isinstance(value['region'], str) or value['region'] not in regions:
        raise ValueError('Unknown BirdCast region.')
    fields(value['metrics'], METRICS)
    fields(value['ranges'], [*METRICS, 'microphone'])
    for source, choices in METRICS.items():
        if value['metrics'][source] not in choices:
            raise ValueError('Unknown graph metric.')
    if any(type(v) is not str or v not in ['30m', '1h', '6h', '12h', '1d', '7d', 'all'] for v in value['ranges'].values()):
        raise ValueError('Unknown graph range.')
    if type(value['animal']) is not str or len(value['animal']) > 256:
        raise ValueError('Invalid animal selection.')
    return copy.deepcopy(value)


def validate_backup(value, regions):
    fields(value, ['format', 'version', 'exported_at', 'shared', 'browser'])
    if value['format'] != FORMAT or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Unsupported configuration format or version.')
    from datetime import datetime
    if not isinstance(value['exported_at'], str):
        raise ValueError('Invalid export timestamp.')
    try:
        stamp = datetime.fromisoformat(value['exported_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ValueError('Invalid export timestamp.') from None
    return {**value, 'shared': validate_shared(value['shared']),
            'browser': validate_browser(value['browser'], regions)}


class ConfigurationStore:
    def __init__(self, root):
        self.path = Path(root) / '.runtime' / 'configuration-v1.json'
        self.shared = dict(DEFAULTS)
        self.revision = uuid.uuid4().hex
        self.error = None
        try:
            value = json.loads(self.path.read_text())
            fields(value, ['format', 'version', 'shared'])
            if value['format'] != FORMAT or type(value['version']) is not int or value['version'] != 1:
                raise ValueError('Unsupported stored configuration version.')
            self.shared = validate_shared(value['shared'])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            self.error = f'Stored configuration could not be loaded; defaults in use. File preserved. {exc}'

    def snapshot(self):
        return dict(shared=dict(self.shared), revision=self.revision, error=self.error)

    def save(self, value, replace_invalid=False):
        value = validate_shared(value)
        if self.error and not replace_invalid:
            raise OSError(self.error + ' Use a confirmed backup restore to replace it.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous = self.path.read_bytes() if self.path.exists() else None
        replaced = False
        fd, name = tempfile.mkstemp(prefix='.configuration-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(dict(format=FORMAT, version=1, shared=value), stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            replaced = True
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            if replaced:
                # Keep the previously accepted file even if directory sync failed.
                if previous is None:
                    self.path.unlink()
                else:
                    with open(name, 'wb') as stream:
                        stream.write(previous)
                        stream.flush()
                    os.replace(name, self.path)
            raise
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.shared = value
        self.revision = uuid.uuid4().hex
        self.error = None
