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
    end = text.index(
        "\n# ------------------------------------------------------------------ user phase"
    )
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


class TestTheInstallerCanFetchARequestedBranch(unittest.TestCase):
    """The user phase could only ever fetch the repo's default branch, so the published
    one-liner — the path every adopter actually takes — was the one path that could not be
    rehearsed before shipping it. A whole fresh-install smoke test had to be run by cloning
    a branch by hand and calling `make setup`, which tests everything except the installer.

    The tarball fallback was worse than unparameterised: it hardcoded `main`, so any fork
    whose default branch is named something else got a working `git clone` and a 404 on the
    fallback — on exactly the hosts that lack git and therefore depend on it.

    Both phases must name the SAME ref. The root phase prints the user-phase command, and
    printing a `main` URL after bootstrapping from a branch would hand the owner an
    installer for different code than the one they just ran.
    """

    def setUp(self):
        self.text = INSTALL_SH.read_text()

    def test_the_ref_defaults_to_main(self):
        self.assertRegex(self.text, r'BUZAI_REF="\$\{BUZAI_REF:-main\}"')

    def test_the_clone_fetches_that_ref(self):
        # both the .git and bare-URL attempts, or the fallback silently ignores the ref
        self.assertEqual(self.text.count('--branch "$BUZAI_REF"'), 2)

    def test_the_tarball_fallback_is_not_pinned_to_main(self):
        self.assertNotIn("archive/refs/heads/main.tar.gz", self.text)
        self.assertIn("archive/refs/heads/$BUZAI_REF.tar.gz", self.text)

    def test_the_printed_next_step_names_the_same_ref(self):
        self.assertNotIn("${BUZAI_REPO}/raw/main/install.sh", self.text)
        self.assertIn("${BUZAI_REPO}/raw/${BUZAI_REF}/install.sh", self.text)

    def test_a_non_default_ref_is_passed_through_to_the_user_phase(self):
        # bootstrapping from a branch must tell the owner to install that same branch
        self.assertIn("BUZAI_REF=${BUZAI_REF} bash", self.text)

    def test_a_default_ref_warns_that_it_cannot_detect_a_branch_bootstrap(self):
        """The dangerous case is the DEFAULT one: bootstrap from a branch URL without
        setting BUZAI_REF, and the printed user-phase command installs main onto an
        account bootstrapped from other code — silently, with no error. curl hands the
        script no URL, so it cannot detect this; it can only say so."""
        self.assertIn("pass BUZAI_REF=<branch> to both phases", self.text)
        # one line, and not phrased as a question aimed at the reader: the adopter
        # following QUICKSTART did not fetch from a branch and should not be asked
        self.assertNotIn("Fetched this script from a branch?", self.text)

    def test_the_variable_is_documented_with_the_others(self):
        self.assertRegex(self.text, r"#\s+BUZAI_REF=<branch>")


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
