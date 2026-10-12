"""English animal names from iNaturalist, with a persistent local cache."""
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from birdcast_scraper import atomic_write_json


class AnimalNames:
    def __init__(self, root):
        self.names = json.loads(Path(__file__).with_name("animal_common_names.json").read_text())
        self.cache = root / ".runtime" / "animal_common_names.json"
        try:
            cached = json.loads(self.cache.read_text())
            self.names.update({name: value for name, value in cached.items()
                               if isinstance(name, str) and (value is None or isinstance(value, str))})
        except (OSError, ValueError, AttributeError):
            pass
        self.lock = threading.Lock()
        self.last_request = 0
        self.failures = {}

    def resolve(self, names):
        with self.lock:
            for name in names:
                if name in self.names or time.monotonic() - self.failures.get(name, float("-inf")) < 60:
                    continue
                time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
                query = urlencode({"q": name, "locale": "en", "per_page": 10})
                request = Request("https://api.inaturalist.org/v1/taxa?" + query,
                                  headers={"User-Agent": "BioGlow field station animal names"})
                try:
                    with urlopen(request, timeout=3) as response:
                        taxa = json.load(response).get("results", [])
                    exact = next((taxon for taxon in taxa if taxon.get("name") == name
                                  and taxon.get("preferred_common_name")), None)
                    if exact is None:
                        exact = next((taxon for taxon in taxa if taxon.get("is_active")
                                      and taxon.get("matched_term") == name), None)
                    common = exact.get("preferred_common_name") if exact else None
                    self.names[name] = common if isinstance(common, str) else None
                    try:
                        atomic_write_json(self.cache, self.names)
                    except OSError:
                        pass
                except (OSError, ValueError, TypeError, AttributeError):
                    self.failures[name] = time.monotonic()
            return {name: self.names.get(name) for name in names}
