"""`install.sh`'s root phase is a contract too, and this holds it to the part hubs need.

A fresh service account has no git committer identity. The hub store is a git repository,
so `make hub-init` refuses to commit without one — and refuses *early*, before moving
anything, which is correct but leaves the documented setup dead-ended: hub-init fails,
personal content stays in the public checkout, and `secrets_preflight` then blocks service
start on exactly that content. Observed end to end on a clean install, which is how it was
found; nothing in the bootstrap or the setup driver had ever set an identity.

Parsing the real script is the same approach `test_service_unit.py` takes with the unit
template, and for the same reason: the artifact is what ships, so the artifact is what is
asserted. The behaviour that needs root (chown) cannot run here, so these tests pin the
properties a reader can verify statically — the guard, the symlink refusal, the ownership
handoff — and the docs are checked for drift alongside.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
HOST_BOOTSTRAP = REPO_ROOT / "docs" / "HOST-BOOTSTRAP.md"


def root_phase() -> str:
    """The body of `root_phase()` — identity work outside it would never run as root."""
    text = INSTALL_SH.read_text()
    start = text.index("root_phase() {")
    end = text.index("\n# ------------------------------------------------------------------ user phase")
    return text[start:end]


class TestTheBootstrapGivesTheAccountAGitIdentity(unittest.TestCase):
    def setUp(self):
        self.phase = root_phase()

    def test_an_identity_is_written_into_the_accounts_gitconfig(self):
        self.assertIn(".gitconfig", self.phase)
        self.assertRegex(self.phase, r"\[user\]")
        self.assertRegex(self.phase, r"name\s*=\s*\$user")
        self.assertRegex(self.phase, r"email\s*=\s*\$user@")

    def test_it_is_skipped_when_one_is_already_set(self):
        # re-running the bootstrap must not append a second [user] section, and must not
        # overwrite an identity the owner chose
        self.assertRegex(self.phase, r"grep -qs .*email.*\$gitconfig")
        self.assertIn("already set in .gitconfig — skipping", self.phase)

    def test_the_target_is_refused_if_it_is_a_symlink(self):
        # every other root-phase write into the account's home does this: the script
        # appends as root, and a symlink would redirect that write out of the home
        self.assertRegex(self.phase, r'refuse_symlink "\$gitconfig"')

    def test_the_file_ends_up_owned_by_the_service_account(self):
        # written as root; left root-owned it would be unreadable to change and wrong
        self.assertRegex(self.phase, r'chown "\$user:\$user" "\$gitconfig"')

    def test_it_runs_before_the_probes_that_can_fail_the_bootstrap(self):
        identity = self.phase.index("$gitconfig")
        probe = self.phase.index("manager probe")
        self.assertLess(identity, probe)


class TestTheDocsDescribeTheSameStep(unittest.TestCase):
    """Docs drift is how a bootstrap step becomes folklore. Both places are checked."""

    def setUp(self):
        self.doc = HOST_BOOTSTRAP.read_text()

    def test_the_numbered_step_list_mentions_it(self):
        self.assertRegex(self.doc, r"\*\*Sets a default git identity")

    def test_the_manual_path_shows_how_to_do_it_by_hand(self):
        manual = self.doc[self.doc.index("Manual path") :]
        self.assertIn(".gitconfig", manual)

    def test_the_step_numbering_has_no_duplicates(self):
        listed = re.findall(r"^(\d+)\. \*\*", self.doc, re.MULTILINE)
        self.assertEqual(listed, sorted(listed, key=int))
        self.assertEqual(len(listed), len(set(listed)), f"duplicate step numbers: {listed}")


if __name__ == "__main__":
    unittest.main()
