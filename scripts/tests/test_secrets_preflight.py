import os
import tempfile
import unittest
from pathlib import Path

from scripts.secrets_preflight import perm_problems, problems, tracked_problems


class TestSecretsPreflight(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mk(self, name, mode):
        p = self.dir / name
        p.write_text("SECRET=x")
        os.chmod(p, mode)
        return p

    def test_0600_passes(self):
        p = self._mk("email.env", 0o600)
        self.assertEqual(perm_problems([p]), [])

    def test_0644_fails(self):
        p = self._mk("email.env", 0o644)
        probs = perm_problems([p])
        self.assertEqual(len(probs), 1)
        self.assertIn("must be 0600", probs[0])

    def test_creds_0644_fails(self):
        p = self._mk(".credentials.json", 0o640)
        self.assertEqual(len(perm_problems([p])), 1)

    def test_missing_file_skipped(self):
        self.assertEqual(perm_problems([self.dir / "nope.env"]), [])

    def test_owner_exec_bit_fails(self):
        # 0o700 has no group/other access but still diverges from 0600 (exec bit)
        p = self._mk("email.env", 0o700)
        probs = perm_problems([p])
        self.assertEqual(len(probs), 1)
        self.assertIn("must be 0600", probs[0])

    def test_tracked_secret_fails(self):
        probs = tracked_problems(["deploy/env/email.env"], is_tracked=lambda p: True)
        self.assertEqual(len(probs), 1)
        self.assertIn("tracked by git", probs[0])

    def test_untracked_secret_passes(self):
        self.assertEqual(tracked_problems(["deploy/env/email.env"], is_tracked=lambda p: False), [])

    # --- managed-connector-only: zero local secrets is valid (not a failure) ---

    def test_no_local_secrets_is_ok_when_creds_clean(self):
        # managed-only adopter: no local *.env files; credentials.json at 0600 → no problems
        creds = self._mk(".credentials.json", 0o600)
        self.assertEqual(problems([], [], creds, is_tracked=lambda p: False), [])

    def test_no_local_secrets_still_checks_creds_perms(self):
        # even with zero local secrets, loose credentials.json perms must still fail
        creds = self._mk(".credentials.json", 0o644)
        probs = problems([], [], creds, is_tracked=lambda p: False)
        self.assertEqual(len(probs), 1)
        self.assertIn("must be 0600", probs[0])

    def test_problems_flags_tracked_repo_secret(self):
        creds = self._mk(".credentials.json", 0o600)
        probs = problems([], ["deploy/env/email.env"], creds, is_tracked=lambda p: True)
        self.assertTrue(any("tracked by git" in p for p in probs))


if __name__ == "__main__":
    unittest.main()
