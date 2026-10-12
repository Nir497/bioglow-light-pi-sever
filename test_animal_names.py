"""Name lookups preserve scientific identity and work with cached data offline."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from animal_names import AnimalNames


class AnimalNameTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.names = AnimalNames(self.root)

    def test_exact_species_lookup_persists_and_ignores_fuzzy_match(self):
        data = {"results": [{"name": "Other species", "preferred_common_name": "Wrong name"},
                            {"name": "Testus example", "preferred_common_name": "Example animal"}]}
        with patch("animal_names.urlopen", return_value=io.BytesIO(json.dumps(data).encode())) as lookup:
            self.assertEqual(self.names.resolve(["Testus example"])["Testus example"], "Example animal")
        self.assertIn("locale=en", lookup.call_args.args[0].full_url)
        with patch("animal_names.urlopen", side_effect=AssertionError("network")):
            self.assertEqual(AnimalNames(self.root).resolve(["Testus example"])["Testus example"], "Example animal")

    def test_scientific_synonym_uses_exact_matched_term(self):
        data = {"results": [{"name": "Testus renamed", "matched_term": "Testus example",
                            "is_active": True, "preferred_common_name": "Example animal"}]}
        with patch("animal_names.urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
            self.assertEqual(self.names.resolve(["Testus example"])["Testus example"], "Example animal")

    def test_network_failure_keeps_scientific_fallback_and_limits_retries(self):
        with patch("animal_names.urlopen", side_effect=OSError("offline")) as lookup:
            self.assertIsNone(self.names.resolve(["Testus example"])["Testus example"])
            self.assertIsNone(self.names.resolve(["Testus example"])["Testus example"])
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(self.names.resolve(["Bubo virginianus"])["Bubo virginianus"], "Great Horned Owl")
