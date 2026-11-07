import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import phase0


class OrchestrationTests(unittest.TestCase):
    def test_group_target_validation(self):
        for value, expected in [("123", "123"), ("https://www.facebook.com/groups/123/?ref=share", "123"),
                                ("https://www.facebook.com/groups/vegmpremium", "vegmpremium")]:
            self.assertEqual(phase0.group_handle(value), expected)
        for value in ("0", "https://facebook.com.evil/groups/123", "https://user:secret@facebook.com/groups/123",
                      "https://www.facebook.com/groups/123/posts/456", "file:///groups/123", "not a URL"):
            with self.subTest(value=value), self.assertRaises(phase0.RunError):
                phase0.group_handle(value)

    def test_env_parser_never_executes_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("FACEBOOK_GROUP_ID=123\nFACEBOOK_COOKIE='c_user=1; xs=$(touch bad)'\nUNRELATED=skip\n")
            with patch.object(phase0, "ROOT", root), patch.dict(os.environ, {}, clear=True):
                phase0.load_local_env()
                self.assertEqual(os.environ["FACEBOOK_COOKIE"], "c_user=1; xs=$(touch bad)")
                self.assertNotIn("UNRELATED", os.environ)
                self.assertFalse((root / "bad").exists())

    def test_group_input_never_persists_query_or_userinfo(self):
        self.assertEqual(phase0.safe_group_input("https://www.facebook.com/groups/vegmpremium?access_token=SECRET#SECRET"),
                         "https://www.facebook.com/groups/vegmpremium")
        self.assertEqual(phase0.safe_group_input("https://user:SECRET@facebook.com/groups/123"), "invalid_group_input")
        self.assertEqual(phase0.safe_group_input("SECRET"), "invalid_group_input")

    def test_postprocess_archives_old_reports_before_failed_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            phase0.write_json(reports / "comparison.json", {"run_id": "old"})
            (reports / "comparison.md").write_text("old report")
            with patch.object(phase0.subprocess, "run", return_value=argparse.Namespace(returncode=2)), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(phase0.postprocess(Path(tmp) / "data", reports, "new"), 2)
            self.assertFalse((reports / "comparison.json").exists())
            self.assertFalse((reports / "comparison.md").exists())
            archives = list(reports.glob("history/*/comparison.json"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(json.loads(archives[0].read_text())["run_id"], "old")

    def test_postprocess_rejects_wrong_report_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            def write_wrong_report(*_args, **_kwargs):
                phase0.write_json(reports / "comparison.json", {"run_id": "wrong"})
                return argparse.Namespace(returncode=0)
            with patch.object(phase0.subprocess, "run", side_effect=write_wrong_report), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(phase0.postprocess(Path(tmp) / "data", reports, "new"), 2)

    def test_missing_auth_writes_both_failed_envelopes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(phase0, "ROOT", Path(tmp)), patch.dict(os.environ, {}, clear=True):
            args = argparse.Namespace(group="https://www.facebook.com/groups/vegmpremium", pages=2,
                                      page_size=20, timeout=120, collector="both", demo=False,
                                      data_dir="data", report_dir="reports")
            with patch.object(phase0, "postprocess", return_value=0), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(phase0.run(args), 1)
            outputs = [json.loads((Path(tmp) / "data/raw" / f"{c}.json").read_text()) for c in phase0.COLLECTORS]
            self.assertEqual(outputs[0]["run_id"], outputs[1]["run_id"])
            for value in outputs:
                self.assertEqual(value["status"], "failed")
                self.assertIsNone(value["group_id"])
                self.assertEqual(value["errors"][0]["code"], "authentication_failed")
                self.assertEqual(value["posts"], [])

    def test_stale_output_is_replaced_on_spawn_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(phase0, "ROOT", Path(tmp)), patch.dict(os.environ, {"FACEBOOK_COOKIE": "secret"}, clear=True):
            output = Path(tmp) / "data/raw/facebook-go.json"
            phase0.write_json(output, {"run_id": "old", "posts": [{"id": "stale"}]})
            args = argparse.Namespace(group="123", pages=2, page_size=20, timeout=120, collector="facebook-go",
                                      demo=False, data_dir="data", report_dir="reports")
            with patch.object(phase0, "resolve_group", return_value="123"), patch.object(phase0.subprocess, "run", side_effect=OSError("secret")), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(phase0.run(args), 1)
            value = json.loads(output.read_text())
            self.assertNotEqual(value["run_id"], "old")
            self.assertEqual(value["posts"], [])
            self.assertNotIn("secret", err.getvalue())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
