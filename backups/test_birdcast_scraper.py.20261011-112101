import json
import tempfile
import unittest
from pathlib import Path

import birdcast_scraper


FIXTURE = """
<html><head><title>California - Migration Dashboard - BirdCast</title></head>
<body>
<h1>Migration Dashboard</h1><h2>California</h2>
<p>Tonight, Oct 4</p>
<p>LIVE DATA FEED Last updated ~17 min. ago</p>
<h2>Total birds</h2>
<p>300 Birds have crossed California so far tonight (est.)</p>
<p>Starting: Sun, Oct 4, 2026, 6:50 PM PDT</p>
<h2>Live migration traffic</h2>
<p>2,100 Birds now in flight (est.)</p>
<p>Direction: Southwest SW</p><p>Speed: 5 mph</p><p>Altitude: 700 ft</p>
<p>Recorded: Sun, Oct 4, 2026, 7:10 PM PDT</p>
<p>Not all birds may fully cross a region, resulting in a greater number of birds in flight than total birds crossed.</p>
<p>Direction, speed, and altitude cannot be reliably determined when the number of birds in flight is low.</p>
</body></html>
"""


class BirdCastScraperTests(unittest.TestCase):
    def test_normalizes_state_inputs(self):
        self.assertEqual(birdcast_scraper.normalize_region("ca"), ("US-CA", "California"))
        self.assertEqual(birdcast_scraper.normalize_region("California")[0], "US-CA")
        with self.assertRaises(birdcast_scraper.ScraperError):
            birdcast_scraper.normalize_region("Canada")

    def test_parses_live_snapshot(self):
        snapshot = birdcast_scraper.parse_snapshot(
            FIXTURE, "US-CA", "https://dashboard.birdcast.org/region/US-CA", "2026-10-04T12:00:00Z"
        )
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["region"]["name"], "California")
        self.assertEqual(snapshot["metrics"]["birds_in_flight"], 2100)
        self.assertEqual(snapshot["metrics"]["total_birds_crossed"], 300)
        self.assertEqual(snapshot["metrics"]["direction"], "SW")
        self.assertEqual(snapshot["metrics"]["speed_mph"], 5)
        self.assertEqual(snapshot["metrics"]["altitude_ft"], 700)
        self.assertEqual(snapshot["metrics"]["recorded_at"], "Sun, Oct 4, 2026, 7:10 PM PDT")
        self.assertTrue(snapshot["warnings"])

    def test_unavailable_snapshot_is_valid(self):
        html = "<title>California - Migration Dashboard - BirdCast</title><p>Migration data are not available for this region on this night.</p>"
        snapshot = birdcast_scraper.parse_snapshot(
            html, "US-CA", "https://dashboard.birdcast.org/region/US-CA", "2026-10-04T12:00:00Z"
        )
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertIsNone(snapshot["metrics"]["birds_in_flight"])
        self.assertIn("unavailable", " ".join(snapshot["warnings"]).lower())

    def test_atomic_output_is_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "snapshot.json"
            payload = {"status": "ok", "metrics": {"birds_in_flight": 1}}
            birdcast_scraper.atomic_write_json(path, payload)
            self.assertEqual(json.loads(path.read_text()), payload)


if __name__ == "__main__":
    unittest.main()
