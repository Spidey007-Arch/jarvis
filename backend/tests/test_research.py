"""JARVIS's own web search: Yahoo and news feeds are parsed into clean results, and anything off-topic or adult is
dropped before it can reach an answer (Bing once returned dinosaur games and adult sites for a K-pop question)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_backend import research  # noqa: E402

YAHOO = (
    '<div class="dd algo algo-sr"><div><a class="d-ib" href="https://r.search.yahoo.com/_ylt=x/RV=2/RU=https%3a%2f%2fweverse.io'
    '%2fweversezone%2fnotice%2f39804/RK=2/RS=y-"><div><span><span>Weverse</span>https://weverse.io › notice</span></div>'
    '<h3 class="title"><span>[Weverse Con Festival] 2027 Weverse Con Festival <b>India</b></span></h3></a></div>'
    '<div class="compText aAbs"><p>For the first time ever, <b>Weverse Con</b> is landing in <b>India</b>.</p></div></div>'
    '<div class="dd algo algo-sr"><a href="https://r.search.yahoo.com/_ylt=y/RU=https%3a%2f%2fchromedino.com%2f/RK=2/">'
    '<h3 class="title"><span>Chrome Dino T-Rex Run</span></h3></a><div class="compText"><p>Play the dinosaur game.</p></div></div>'
)

NEWS = (
    "<rss><channel><item><title>Weverse Con Festival India pre-sale triggers fan outrage - Livemint</title>"
    "<link>https://news.google.com/rss/articles/abc</link><pubDate>Tue, 27 Oct 2026 08:00:00 GMT</pubDate>"
    "<source url=\"https://www.livemint.com\">Livemint</source></item>"
    "<item><title>Stock markets close higher</title><link>https://example.com/x</link></item></channel></rss>"
)

Q = "HYBE Weverse Con Festival India"


class Search(unittest.TestCase):
    def test_yahoo_results_are_clean_and_on_topic(self) -> None:
        found = research.parse_yahoo(YAHOO)
        self.assertEqual(found[0]["url"], "https://weverse.io/weversezone/notice/39804")
        self.assertEqual(found[0]["title"], "[Weverse Con Festival] 2027 Weverse Con Festival India")  # no site name or address
        self.assertIn("Weverse Con is landing in India", found[0]["snippet"])
        self.assertEqual([r["url"] for r in research.relevant(found, Q)], ["https://weverse.io/weversezone/notice/39804"])

    def test_adult_and_off_topic_results_are_dropped(self) -> None:
        junk = [{"title": "Weverse India videos", "url": "https://xnxx.com/search/weverse-india", "snippet": "weverse con festival"},
                {"title": "Facebook - log in", "url": "https://www.facebook.com/", "snippet": ""},
                {"title": "Weverse Con Festival India dates", "url": "https://www.indiatoday.in/x", "snippet": ""}]
        self.assertEqual([r["url"] for r in research.relevant(junk, Q)], ["https://www.indiatoday.in/x"])

    def test_news_headlines_keep_date_and_source(self) -> None:
        items = research.parse_news(NEWS)
        self.assertEqual(items[0]["source"], "Livemint")
        self.assertTrue(items[0]["date"].startswith("Tue, 27 Oct 2026"))
        on_topic = research.relevant([{**i, "snippet": ""} for i in items], Q)
        self.assertEqual(len(on_topic), 1)


if __name__ == "__main__":
    unittest.main()
