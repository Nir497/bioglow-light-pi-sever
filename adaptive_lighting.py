"""Pure adaptive policy. Percentages are prototype settings, not wildlife-safe limits."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path

MIGRATION_CAPS = {'low': 60, 'medium': 40, 'high': 20, 'very_high': 10}
MIN_BRIGHTNESS = 10
MAX_BRIGHTNESS = 60
MAX_DISTANCE_M = 5.0


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class AdaptiveConfig:
    min_brightness: float = MIN_BRIGHTNESS
    max_brightness: float = MAX_BRIGHTNESS
    max_distance_m: float = MAX_DISTANCE_M
    dark_lux: float = 15
    bright_lux: float = 25
    lux_stale_seconds: float = 3
    presence_timeout_seconds: float = 10
    off_fade_seconds: float = 0.5
    smoothing_tau_seconds: float = -0.1 / math.log(0.8)
    lux_filter_tau_seconds: float = 1
    migration_stale_seconds: float = 1800
    migration_region: str = 'US-CA'
    require_motion_to_activate: bool = True

    def __post_init__(self):
        values = asdict(self)
        region = values.pop('migration_region')
        require_motion = values.pop('require_motion_to_activate')
        if type(require_motion) is not bool:
            raise ValueError('require_motion_to_activate must be a boolean')
        if any(not finite(v) for v in values.values()):
            raise ValueError('Adaptive settings must be finite numbers')
        if not 0 <= self.min_brightness <= self.max_brightness <= 100:
            raise ValueError('Require 0 <= min_brightness <= max_brightness <= 100')
        if not 0 <= self.dark_lux < self.bright_lux:
            raise ValueError('Require 0 <= dark_lux < bright_lux')
        if self.presence_timeout_seconds < 0 or any(values[k] <= 0 for k in (
                'max_distance_m', 'lux_stale_seconds', 'smoothing_tau_seconds',
                'lux_filter_tau_seconds', 'migration_stale_seconds', 'off_fade_seconds')):
            raise ValueError('Adaptive durations and range must be positive')
        if not isinstance(region, str) or not region.startswith('US-'):
            raise ValueError('migration_region must be a BirdCast US region code')

    @classmethod
    def load(cls, root):
        path = Path(root) / 'adaptive-lighting.json'
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text()))


def distance_target(distance_m, config=None):
    config = config or AdaptiveConfig()
    if not finite(distance_m) or not 0 <= distance_m <= config.max_distance_m:
        return 0
    return config.min_brightness + (config.max_brightness - config.min_brightness) * (
        1 - distance_m / config.max_distance_m)


def calculate_brightness(is_dark, presence_detected, person_distance_m, migration_level, config=None):
    if not is_dark or not presence_detected:
        return 0
    return round(min(distance_target(person_distance_m, config), MIGRATION_CAPS.get(migration_level, 10)))


def nearest_distance(reading, max_distance_m):
    """Nearest reported active radar channel, not a human or multi-person tracker."""
    presence = reading.get('presence')
    keys = {'moving': ('moving_distance_cm',), 'stationary': ('stationary_distance_cm',),
            'both': ('moving_distance_cm', 'stationary_distance_cm')}.get(presence, ())
    candidates = [reading.get(key) for key in keys]
    valid = [v / 100 for v in candidates if finite(v) and 0 <= v <= max_distance_m * 100]
    return min(valid) if valid else None


def migration_from_snapshot(snapshot, config, now=None):
    """Accept only documented live High badge; counts do not imply categories."""
    now = now or datetime.now(timezone.utc)
    try:
        metrics = snapshot['metrics']
        recorded = datetime.fromisoformat(metrics['recorded_at_utc'].replace('Z', '+00:00'))
        retrieved = datetime.fromisoformat(snapshot['retrieved_at'].replace('Z', '+00:00'))
        ages = [(now - stamp).total_seconds() for stamp in (recorded, retrieved)]
        valid = (snapshot['status'] == 'ok' and snapshot['region']['code'] == config.migration_region
                 and snapshot.get('live_feed') is True and all(0 <= age <= config.migration_stale_seconds for age in ages))
        if valid and snapshot.get('migration_level') == 'high':
            return 'high', max(ages), None
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return 'unknown', None, 'Migration classification unavailable, stale, historical, or wrong region; using 10% cap.'


class AdaptivePolicy:
    def __init__(self, config=None):
        self.config = config or AdaptiveConfig()
        self.reset()

    def reset(self):
        self.output = 0.0
        self.last_tick = None
        self.lost_at = None
        self.held = 0.0

    def step(self, now, dark, sensor_valid, distance, migration_level):
        cap = MIGRATION_CAPS.get(migration_level, 10)
        dt = 0.1 if self.last_tick is None else max(0, now - self.last_tick)
        self.last_tick = now
        if not sensor_valid:
            distance = None
        requested = distance_target(distance, self.config)
        state = 'tracking'
        if not dark or not sensor_valid:
            self.output = self.held = 0.0
            self.lost_at = None
            target = 0.0
            state = 'sensor_unavailable' if not sensor_valid else 'ambient_off'
        else:
            if distance is not None:
                self.lost_at = None
                target = min(requested, cap)
                self.held = target
            else:
                if self.lost_at is None:
                    self.lost_at = now
                    # Hold the last permitted output, not an unreached target.
                    self.held = min(self.output, self.held)
                self.held = min(self.held, cap)
                holding = now - self.lost_at < self.config.presence_timeout_seconds
                target = self.held if holding else 0.0
                state = 'presence_hold' if holding and target else 'fading_off' if self.output else 'off'
            if state == 'fading_off':
                elapsed = max(0, now - self.lost_at - self.config.presence_timeout_seconds)
                self.output = min(self.output, self.held * max(0, 1 - elapsed / self.config.off_fade_seconds))
            else:
                self.output += (1 - math.exp(-dt / self.config.smoothing_tau_seconds)) * (target - self.output)
            self.output = min(self.output, cap, target if state == 'presence_hold' else 100)
            if self.output < 0.1:
                self.output = 0.0
        return dict(presence_detected=distance is not None, person_distance_m=distance,
                    distance_target_percent=round(requested, 3), migration_level=migration_level,
                    migration_cap_percent=cap, final_target_percent=round(target, 3),
                    output_percent=round(self.output, 3), lighting_state=state,
                    presence_timeout_remaining_seconds=None if self.lost_at is None else max(
                        0, self.config.presence_timeout_seconds - (now - self.lost_at)))
