import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import fetch


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vid = "ABcd123_X-y"
        self.output = self.root / "downloads"

    def run_fetch(self, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fetch.main(["--output", str(self.output), *args])

    def test_report_and_urls_reuse_the_same_video_id(self):
        report = self.root / "rank.json"
        report.write_text(json.dumps({"videos": [{"id": self.vid}]}))
        def fake_run(command, **kwargs):
            directory = Path(command[command.index("-o") + 1]).parent
            self.assertEqual(command[-1], f"https://www.youtube.com/watch?v={self.vid}")
            (directory / f"{self.vid}.mp4").write_bytes(b"video")
            (directory / f"{self.vid}.info.json").write_text(json.dumps({"id": self.vid}))
            return SimpleNamespace(returncode=0)
        with patch.object(fetch.subprocess, "run", side_effect=fake_run) as downloader:
            with patch.object(fetch, "duration_of", return_value=1.0):
                self.assertEqual(self.run_fetch(["--from-report", str(report), "--no-sheets",
                                                 f"https://youtu.be/{self.vid}"]), 0)
                self.assertEqual(self.run_fetch(["--no-sheets", self.vid]), 0)
        self.assertEqual(downloader.call_count, 1)
        self.assertEqual((self.output / "sources" / f"{self.vid}.mp4").read_bytes(), b"video")
        self.assertTrue((self.output / "sources" / f"{self.vid}.info.json").is_file())

    def test_mismatched_cache_identity_returns_failure_without_installing_video(self):
        cache = self.root / "cache"
        cache.mkdir()
        (cache / f"{self.vid}.mp4").write_bytes(b"cached")
        (cache / f"{self.vid}.info.json").write_text(json.dumps({"id": "wrongsource"}))
        self.assertEqual(self.run_fetch(["--cache", str(cache), self.vid]), 1)
        self.assertFalse((self.output / "sources" / f"{self.vid}.mp4").exists())

    def test_failed_download_is_retryable(self):
        with patch.object(fetch.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            self.assertEqual(self.run_fetch([self.vid]), 1)
        self.assertFalse((self.output / "sources" / f"{self.vid}.mp4").exists())
        self.assertTrue((self.output / "inspection" / f"{self.vid}.download.log").is_file())

    def test_ids_are_restricted_to_youtube(self):
        self.assertEqual(fetch.video_id(f"https://youtube.com/shorts/{self.vid}"), self.vid)
        for value in ["../escape.mp4", f"https://example.org/watch?v={self.vid}"]:
            with self.assertRaises(ValueError):
                fetch.video_id(value)
