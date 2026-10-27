"""Documents on this PC: create Word, Excel, PowerPoint and PDF files, read them back with their locations,
find the passage that answers a question, and keep files in JARVIS's own store. No AI service involved."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_agent import actions, docs  # noqa: E402

REPORT = """# Quarterly report
Revenue grew by 18 percent in the third quarter, driven by new customers in Pune.

## Risks
- Supplier delays in October
- Higher cloud costs

| Region | Sales |
|---|---|
| Pune | 120 |
| Mumbai | 95 |
"""


class Documents(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self.dir.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.dir.cleanup()

    def make(self, kind: str) -> Path:
        out = actions.run("create_document", {"kind": kind, "title": "Q3", "content": REPORT, "path": str(Path(self.dir.name) / "out" / "q3")})
        p = Path(out["created"])
        self.assertTrue(p.is_file() and p.suffix == f".{kind}")
        return p

    def test_word_pdf_and_powerpoint_read_back_with_locations(self) -> None:
        for kind, where in (("docx", "section 1"), ("pdf", "p. 1"), ("pptx", "slide")):
            p = self.make(kind)
            out = actions.run("read_document", {"file": str(p), "question": "how much did revenue grow"})
            first = out["passages"][0]
            self.assertIn("18 percent", " ".join(x["text"] for x in out["passages"]), kind)
            self.assertTrue(first["where"].startswith(where), (kind, first["where"]))

    def test_excel_sheets_and_numbers(self) -> None:
        p = Path(actions.run("create_document", {"kind": "excel", "content": "# Sales\nRegion, Sales\nPune, 120\nMumbai, 95\n# Costs\n| Item | Cost |\n|---|---|\n| Cloud | 40 |",
                                                  "path": str(Path(self.dir.name) / "s.xlsx")})["created"])
        import openpyxl

        wb = openpyxl.load_workbook(p)
        self.assertEqual(wb.sheetnames, ["Sales", "Costs"])
        self.assertEqual(wb["Sales"]["B2"].value, 120)
        out = actions.run("read_document", {"file": str(p), "pages": "2"})
        self.assertEqual(out["passages"][0]["where"], "sheet Costs")

    def test_never_overwrites(self) -> None:
        a, b = self.make("docx"), self.make("docx")
        self.assertNotEqual(a, b)
        self.assertTrue(b.stem.endswith("(2)"))

    def test_store_once_then_ask_by_name(self) -> None:
        p = self.make("pdf")
        stored = actions.run("store_file", {"path": str(p)})
        self.assertEqual(actions.run("store_file", {"path": str(p)})["already_stored"], True)
        p.unlink()  # the original can go away: the store has its own copy and text
        out = actions.run("read_document", {"file": stored["id"], "question": "risks supplier"})
        self.assertIn("Supplier delays", " ".join(x["text"] for x in out["passages"]))
        self.assertEqual(actions.run("forget_file", {"id": stored["id"]})["removed"], "q3.pdf")
        self.assertEqual(actions.run("stored_files", {})["files"], [])

    def test_markdown_blocks(self) -> None:
        kinds = [k for k, _ in docs._blocks(REPORT)]
        self.assertEqual(kinds, ["h1", "p", "h2", "bullet", "bullet", "table"])
        self.assertEqual(docs._page_range("1, 3-4", 10), {1, 3, 4})


if __name__ == "__main__":
    unittest.main()
