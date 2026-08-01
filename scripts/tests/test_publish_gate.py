import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.publish_gate import GENERIC_PATTERNS, load_private_patterns, scan_files


class TestScanFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_clean_tree_yields_no_findings(self):
        p = self._write("docs/ok.md", "nothing to see here\n")
        self.assertEqual(scan_files([p], GENERIC_PATTERNS, root=self.root), [])

    def test_violation_reports_path_and_line(self):
        p = self._write("docs/bad.md", "fine\nsee buzai-exp-SESSION-HANDOFF.md for notes\n")
        findings = scan_files([p], GENERIC_PATTERNS, root=self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("docs/bad.md:2", findings[0])

    def test_private_patterns_extend_coverage(self):
        p = self._write("docs/infra.md", "box lives at server9.example.internal\n")
        # generic set alone: clean; with a private pattern: caught
        self.assertEqual(scan_files([p], GENERIC_PATTERNS, root=self.root), [])
        findings = scan_files(
            [p], GENERIC_PATTERNS + [r"server9\.example\.internal"], root=self.root
        )
        self.assertEqual(len(findings), 1)

    def test_exempt_policy_file_may_contain_patterns(self):
        p = self._write("docs/dev/PUBLIC-SEED.md", "never reference SESSION-HANDOFF.md files\n")
        self.assertEqual(scan_files([p], GENERIC_PATTERNS, root=self.root), [])

    def test_binary_file_skipped(self):
        p = self.root / "blob.bin"
        p.write_bytes(b"\x00\xff\x00SESSION-HANDOFF.md\xff")
        self.assertEqual(scan_files([p], GENERIC_PATTERNS, root=self.root), [])


class TestLoadPrivatePatterns(unittest.TestCase):
    def test_missing_file_is_normal(self):
        pats, present = load_private_patterns("/nonexistent/gate-patterns")
        self.assertEqual((pats, present), ([], False))

    def test_comments_and_blanks_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("# literal infra names\nmybox\\.example\\.com\n\n#skip\nuser@realhost\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        pats, present = load_private_patterns(path)
        self.assertTrue(present)
        self.assertEqual(pats, [r"mybox\.example\.com", "user@realhost"])


class TestRepoIsClean(unittest.TestCase):
    def test_generic_patterns_clean_on_this_repo(self):
        # The real gate over the real tracked tree must be green — this is the CI seed check.
        # Generic set only: point the private-file env at a nonexistent path so the test
        # is deterministic across machines.
        from scripts.publish_gate import main

        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
            mock.patch.dict(os.environ, {"BUZAI_GATE_PATTERNS": "/nonexistent"}),
        ):
            rc = main([])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
