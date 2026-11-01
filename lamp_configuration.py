"""Durable user preferences, separate from live lamp/controller state."""
import copy
import json
import logging
import re

from birdcast_scraper import atomic_write_json


class ConfigurationSaveError(OSError):
    pass


class LampConfiguration:
    DEFAULTS = {"alignment": 144, "beam_width": 1, "offset": 0,
                "manual_brightness": 255, "color": "#ffa028"}

    def __init__(self, root):
        self.path = root / ".runtime" / "lamp_config.json"
        self.settings = dict(self.DEFAULTS)
        self.persistence_status = "defaults"
        self.error = None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
                raise ValueError("Unsupported lamp configuration version")
            self.settings = self.validate(payload["settings"])
            self.persistence_status = "saved"
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.persistence_status = "error"
            self.error = f"Could not load lamp settings; using defaults: {exc}"
            logging.getLogger(__name__).warning(self.error)

    @classmethod
    def validate(cls, settings):
        if not isinstance(settings, dict) or set(settings) != set(cls.DEFAULTS):
            raise ValueError("Lamp configuration must contain all five settings")
        spacing = settings["alignment"]
        if type(spacing) is not int or not 1 <= spacing <= 144:
            raise ValueError("alignment must be an integer from 1 to 144")
        width = settings["beam_width"]
        if type(width) is not int or not 1 <= width <= min(143, 2 * (spacing // 2) + 1) or width % 2 != 1:
            raise ValueError("beam_width must be an odd integer within the alignment limit")
        offset = settings["offset"]
        if type(offset) is not int or not 0 <= offset < spacing:
            raise ValueError("offset must be an integer within the alignment spacing")
        brightness = settings["manual_brightness"]
        if type(brightness) is not int or not 1 <= brightness <= 255:
            raise ValueError("brightness must be an integer from 1 to 255")
        color = settings["color"]
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("color must be a hex RGB value such as #ff8000")
        return {**settings, "color": color.lower()}

    def save(self, changes):
        candidate = self.validate({**self.settings, **changes})
        if candidate == self.settings and self.persistence_status == "saved":
            return
        try:
            atomic_write_json(self.path, {"version": 1, "settings": candidate})
        except OSError as exc:
            self.persistence_status = "error"
            self.error = f"Could not save lamp settings: {exc}"
            raise ConfigurationSaveError(self.error) from exc
        self.settings = candidate
        self.persistence_status = "saved"
        self.error = None

    def status(self):
        return {"settings": copy.deepcopy(self.settings),
                "persistence_status": self.persistence_status, "error": self.error}
