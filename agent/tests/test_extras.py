"""System extras: finished browser downloads are reported once, partial ones never; programs are flagged."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[2] / "common")]

from jarvis_agent import extras  # noqa: E402


class Downloads(unittest.TestCase):
    def test_reports_each_finished_file_once(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.object(extras, "_downloads_dir", lambda: Path(d)):
            (Path(d) / "old.pdf").write_bytes(b"x")
            got: list[dict] = []
            w = extras.DownloadWatcher(got.append)
            w._seen = {p.name for p in w._scan()}
            (Path(d) / "movie.mp4.crdownload").write_bytes(b"xx")
            (Path(d) / "report.pdf").write_bytes(b"abc")
            (Path(d) / "setup.exe").write_bytes(b"abcd")
            w.tick()
            self.assertEqual(got, [])  # not yet seen unchanged for a whole poll
            w.tick()
            w.tick()
            self.assertEqual(sorted(e["name"] for e in got), ["report.pdf", "setup.exe"])
            self.assertEqual({e["name"]: e["program"] for e in got}, {"report.pdf": False, "setup.exe": True})


if __name__ == "__main__":
    unittest.main()
