"""Public APIs: the whole list is searchable, parameters parse, and the caller can't reach this PC or the LAN."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend import public_apis as p  # noqa: E402
from jarvis_backend.settings import SettingsStore  # noqa: E402


class Catalog(unittest.TestCase):
    def test_every_api_is_there(self) -> None:
        apis = p.catalog()
        self.assertGreater(len(apis), 1400)
        self.assertGreaterEqual(len(p.categories()), 45)
        self.assertTrue(all(a["name"] and a["link"].startswith("http") for a in apis))
        for name in ("Open-Meteo", "CoinGecko", "NASA", "Frankfurter"):
            self.assertIsNotNone(p.lookup(name), name)

    def test_search_ranks_by_topic_and_usability(self) -> None:
        r = p.find("weather", no_key_only=True)
        self.assertTrue(r["matches"])
        self.assertTrue(all(m["needs"] == "nothing" for m in r["matches"]))
        self.assertTrue(all("Weather" in m["category"] or "weather" in m["description"].lower() for m in r["matches"]))

    def test_saved_key_counts_as_usable(self) -> None:
        r = p.find("OpenWeatherMap", no_key_only=True, has_key=lambda n: n == "OpenWeatherMap")
        self.assertEqual(r["matches"][0]["name"], "OpenWeatherMap")
        self.assertTrue(r["matches"][0]["key_saved"])

    def test_params(self) -> None:
        self.assertEqual(p.parse_params('{"q": "Pune", "n": 2}'), {"q": "Pune", "n": 2})
        self.assertEqual(p.parse_params("?q=Pune&units=metric"), {"q": "Pune", "units": "metric"})
        self.assertEqual(p.parse_params(None), {})


class Reach(unittest.TestCase):
    def test_local_addresses_are_refused(self) -> None:
        for url in ("http://127.0.0.1:8765/health", "http://localhost/", "http://10.0.0.1/", "http://192.168.1.1/",
                    "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://printer.local/", "ftp://example.com/x"):
            with self.assertRaises(p.APIError, msg=url):
                asyncio.run(p.call(url))


class Keys(unittest.TestCase):
    def test_service_keys_are_stored_apart(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            s = SettingsStore(Path(d) / "settings.dat")
            s.set_service_key("OpenWeatherMap", "abc123")
            self.assertEqual(s.service_key("openweathermap"), "abc123")
            self.assertEqual(s.service_keys(), ["openweathermap"])
            self.assertNotIn("abc123", str(s.public()))
            s.set_service_key("OpenWeatherMap", "")
            self.assertEqual(s.service_keys(), [])


if __name__ == "__main__":
    unittest.main()
