"""Downloads: file names are made safe, and programs or scripts are never fetched."""
from __future__ import annotations

import io
import sys
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_agent import actions  # noqa: E402


def response(url: str, ctype: str, disposition: str = "", body: bytes = b"data"):
    headers = Message()
    headers["Content-Type"] = ctype
    if disposition:
        headers["Content-Disposition"] = disposition
    r = io.BytesIO(body)
    r.headers, r.geturl = headers, (lambda: url)
    return r


class Names(unittest.TestCase):
    def test_from_url_and_header(self) -> None:
        self.assertEqual(actions._download_name("https://x.org/a/report%20Q3.pdf?x=1", ""), "report Q3.pdf")
        self.assertEqual(actions._download_name("https://x.org/dl", 'attachment; filename="photo.jpg"'), "photo.jpg")
        self.assertEqual(actions._download_name("https://x.org/dl", "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"), "résumé.pdf")
        self.assertEqual(actions._download_name("https://x.org/..%5C..%5Cwin.ini", ""), "_.._win.ini")


class Refusals(unittest.TestCase):
    def fetch(self, url, ctype, disposition=""):
        with mock.patch("urllib.request.urlopen", return_value=response(url, ctype, disposition)):
            return actions.download_file({"url": url, "open": False})

    def test_programs_and_scripts(self) -> None:
        for url, ctype, disp in [("https://x.org/setup.exe", "application/octet-stream", ""),
                                 ("https://x.org/dl", "application/octet-stream", 'attachment; filename="run.ps1"'),
                                 ("https://x.org/dl", "application/x-msdownload", 'attachment; filename="doc.pdf"'),
                                 ("https://x.org/invoice.pdf.lnk", "application/octet-stream", "")]:
            with self.assertRaises(actions.ActionError, msg=url + disp):
                self.fetch(url, ctype, disp)

    def test_web_page(self) -> None:
        with self.assertRaises(actions.ActionError):
            self.fetch("https://x.org/article", "text/html")


if __name__ == "__main__":
    unittest.main()
