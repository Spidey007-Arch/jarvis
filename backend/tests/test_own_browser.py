"""Web pages and searches stay in JARVIS's own hidden browser; search keeps working when engines refuse plain requests."""
import asyncio
import base64
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "common"), str(ROOT / "agent")]

from jarvis_backend import research  # noqa: E402

BING = """<ol id="b_results"><li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;p=x&amp;u=a1{u}&amp;ntb=1">EvoFox <strong>Typhoon 2</strong> Cooling Pad</a></h2>
<div class="b_caption"><p>Six fans, RGB lights and a metal top.</p></div></li>
<li class="b_algo"><h2><a href="https://www.flipkart.com/evofox-typhoon-2/p/1">EVOFOX Typhoon 2 on Flipkart</a></h2><p>Adjustable height.</p></li></ol>"""


def tracked(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


class SearchTests(unittest.TestCase):
    def tearDown(self) -> None:
        research.BROWSER = None

    def test_bing_results_are_read_and_unwrapped(self) -> None:
        got = research.parse_bing(BING.format(u=tracked("https://www.amazon.in/EvoFox-Typhoon-2/dp/B0G48ZZHLZ")))
        self.assertEqual([x["url"] for x in got], ["https://www.amazon.in/EvoFox-Typhoon-2/dp/B0G48ZZHLZ", "https://www.flipkart.com/evofox-typhoon-2/p/1"])
        self.assertEqual(got[0]["title"], "EvoFox Typhoon 2 Cooling Pad")
        self.assertEqual(got[0]["snippet"], "Six fans, RGB lights and a metal top.")

    def test_when_every_engine_refuses_it_searches_in_jarvis_browser(self) -> None:
        asked = []

        async def browser(action, args):
            asked.append(action)
            return {"results": [{"title": "EvoFox Typhoon 2 Laptop Cooling Pad", "url": "https://www.amazon.in/x", "snippet": "6 fans"}]}

        class Refuses:
            status_code, text = 500, ""

        async def request(*_a, **_k):
            return Refuses()

        research.BROWSER = browser
        client = mock.Mock(request=request)
        with mock.patch.object(research.asyncio, "sleep", mock.AsyncMock()):
            engine, found = asyncio.run(research._search(client, "EvoFox Typhoon 2 cooling pad", 5))
        self.assertEqual(engine, "JARVIS's browser")
        self.assertEqual(found[0]["url"], "https://www.amazon.in/x")
        self.assertEqual(asked, ["browser_search"])

    def test_pages_that_refuse_plain_requests_are_read_in_jarvis_browser(self) -> None:
        async def browser(action, args):
            return {"text": f"Text of {args['url']}"}

        research.BROWSER = browser
        texts = asyncio.run(research._read_refused([{"url": "https://example.com/1"}, {"url": "https://example.com/2"}], ["already read", None]))
        self.assertEqual(texts, ["already read", "Text of https://example.com/2"])


class OwnBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        if sys.platform != "win32":
            self.skipTest("the desktop agent runs on Windows")
        from jarvis_agent import actions

        self.actions = actions

    def test_pages_open_in_jarvis_browser_unless_a_browser_is_named(self) -> None:
        a = self.actions
        with mock.patch.object(a.webdrive, "show_page", return_value={"title": ""}) as page, \
             mock.patch.object(a, "_open_url", return_value="brave") as outside:
            out = a.open_url({"url": "github.com"})
            self.assertEqual(out["in"], "JARVIS's browser")
            page.assert_called_once_with("https://github.com")
            outside.assert_not_called()
            a.web_search({"query": "evofox typhoon 2"})
            self.assertEqual(page.call_count, 2)  # the same reused tab: nothing piles up
            self.assertEqual(a.open_url({"url": "github.com", "browser": "brave"})["in"], "brave")
            outside.assert_called_once()

    def test_the_browser_panel_never_opens_by_itself(self) -> None:
        src = (ROOT / "src" / "api.ts").read_text(encoding="utf-8")
        self.assertIn('BROWSER_TOOLS: Record<string, "show" | "hide"> = { hide_browser: "hide" }', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
