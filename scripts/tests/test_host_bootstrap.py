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

import os
import re
import subprocess
import tempfile
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


class TestConsentStepProbesLikeEveryOtherStep(unittest.TestCase):
    """The driver promises it "probes each step, skips what's already done, pauses only
    where a human is required". Step 7 did not probe: it printed instructions for a
    command needing the very shell its prompt was blocking, then asked "Done (or not
    needed)?" — a question whose only truthful first-pass answer is N.

    The consent dialog records itself account-wide in ~/.claude.json. Measured both ways
    on real installs: absent on a fresh account that genuinely needed priming, true on one
    whose service then registered without it.
    """

    def setUp(self):
        self.text = INSTALL_SH.read_text()

    def test_there_is_a_probe_for_recorded_consent(self):
        self.assertIn("consent_recorded()", self.text)
        self.assertIn("remoteDialogSeen", self.text)

    def test_step_seven_uses_it_to_skip(self):
        self.assertIn("if consent_recorded; then", self.text)
        self.assertIn("consent already recorded", self.text)

    def test_the_pause_says_to_answer_N_first(self):
        # it cannot be satisfied from the prompt, so it must not ask as though it can
        self.assertIn("answer N below", self.text)

    def test_the_pause_still_names_the_command_and_the_resume(self):
        self.assertIn("make prime-consent", self.text)
        self.assertIn("make setup", self.text)


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


# The section an owner with an old ~/buzai install is sent to.
MIGRATION = "Moving an existing install to ~/buzai-assistant"


class TestTheUserPhaseInstallsIntoBuzaiAssistant(unittest.TestCase):
    """Issue #17. The Remote Control picker labels an environment by its working
    directory's basename, so a deployment at ~/buzai looks the same as a dev checkout of
    the repo. New installs go to ~/buzai-assistant. A host that already has ~/buzai is
    stopped and pointed at the migration section; a second clone beside it would leave
    two checkouts and a unit serving the old one.

    These run the real user phase in a scratch HOME, from a scratch directory that is not
    a checkout. `git` and `make` are stubs that record their calls, and the admin-account
    guard is waived through its own documented opt-out, so it stays intact. Under root
    the script would take its root phase instead, which must never run from a test."""

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("install.sh runs its root phase as root; never from a test")
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.home, self.cwd, stubs = tmp / "home", tmp / "cwd", tmp / "stubs"
        for d in (self.home, self.cwd, stubs):
            d.mkdir()
        self.git_log, self.make_log = tmp / "git.log", tmp / "make.log"
        # the stub clone creates its target (the last argument), as a real one would
        self._stub(
            stubs / "git", f'echo "$@" >> "{self.git_log}"; for a; do t="$a"; done; mkdir -p "$t"'
        )
        self._stub(stubs / "make", f'echo "$(pwd) $*" >> "{self.make_log}"')
        self.path = f"{stubs}:/usr/bin:/bin"

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _stub(path: Path, body: str):
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def checkout(self, name: str) -> Path:
        root = self.home / name
        (root / "trust").mkdir(parents=True)
        (root / "Makefile").write_text("")
        return root

    def user_phase(self, **extra: str) -> subprocess.CompletedProcess:
        env = {"HOME": str(self.home), "PATH": self.path, "BUZAI_ALLOW_ADMIN_INSTALL": "1", **extra}
        return subprocess.run(
            ["bash", str(INSTALL_SH)],
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def logged(self, log: Path) -> str:
        return log.read_text() if log.exists() else ""

    def test_a_fresh_account_clones_into_buzai_assistant(self):
        run = self.user_phase()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(f"{self.home}/buzai-assistant", self.logged(self.git_log))
        self.assertTrue(self.logged(self.make_log).startswith(f"{self.home}/buzai-assistant setup"))

    def test_an_old_buzai_install_is_stopped_and_pointed_at_the_migration(self):
        self.checkout("buzai")
        run = self.user_phase()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(MIGRATION, run.stderr)
        self.assertIn("Nothing was moved", run.stderr)
        self.assertEqual(self.logged(self.git_log), "", "it must not clone beside the old install")
        self.assertEqual(self.logged(self.make_log), "")

    def test_the_keep_it_remedy_is_a_command_that_works(self):
        # `BUZAI_WORKDIR=… curl … | bash` sets the variable for curl, not bash, so a hint
        # phrased as an env var loops the owner straight back into this stop
        old = self.checkout("buzai")
        run = self.user_phase()
        self.assertIn(f"cd {old} && make setup", run.stderr)
        self.assertNotIn("re-run with BUZAI_WORKDIR", run.stderr)

    def test_an_empty_workdir_is_not_a_chosen_location(self):
        # an empty value falls back to the default path, so it must not skip the stop
        self.checkout("buzai")
        run = self.user_phase(BUZAI_WORKDIR="")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(MIGRATION, run.stderr)
        self.assertEqual(self.logged(self.git_log), "")

    def test_an_owner_who_names_the_old_path_keeps_it(self):
        old = self.checkout("buzai")
        run = self.user_phase(BUZAI_WORKDIR=str(old))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.logged(self.git_log), "")
        self.assertTrue(self.logged(self.make_log).startswith(f"{old} setup"))

    def test_an_already_migrated_host_continues_in_buzai_assistant(self):
        self.checkout("buzai")
        new = self.checkout("buzai-assistant")
        run = self.user_phase()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(self.logged(self.make_log).startswith(f"{new} setup"))

    def test_the_documented_default_matches(self):
        documented = "(default: $HOME/buzai-assistant)" in INSTALL_SH.read_text()
        self.assertTrue(documented, "the BUZAI_WORKDIR usage line names another default")


def shell_function(name: str) -> str:
    """One function's definition, cut from the install.sh text. Sourcing the script
    would run its entry `case`, so the function is lifted out and run on its own."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", INSTALL_SH.read_text(), re.M | re.S)
    if match is None:
        raise AssertionError(f"install.sh defines no {name}()")
    return match.group(0)


class TestSetupNoticesAUnitServingAnotherCheckout(unittest.TestCase):
    """Issue #17. Setup's service step passed whenever a unit file existed. After a move,
    or on a host with two checkouts, that unit can serve a different checkout from the
    one being set up, and setup would report it done. Steps 1-7 need real credentials,
    so the comparison lives in a helper that is run here on its own.

    The unit on disk usually carries the unsubstituted `%h/...` value: `make
    service-install` leaves the template's path alone when the checkout is the default,
    and every legacy unit reads `%h/buzai`. A raw string compare would refuse them all."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.unit = self.home / ".config" / "systemd" / "user" / "claude-remote.service"
        self.unit.parent.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def serves(self, working_directory: str, checkout: str) -> bool:
        self.unit.write_text(f"[Service]\nWorkingDirectory={working_directory}\n")
        here = self.home / checkout
        here.mkdir(exist_ok=True)
        script = shell_function("unit_workdir") + shell_function("unit_serves_this_checkout")
        run = subprocess.run(
            ["bash", "-c", f"{script}\nunit_serves_this_checkout"],
            cwd=here,
            env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        return run.returncode == 0

    def test_the_default_unsubstituted_unit_serves_buzai_assistant(self):
        self.assertTrue(self.serves("%h/buzai-assistant", "buzai-assistant"))

    def test_a_legacy_unit_still_serves_its_buzai_checkout(self):
        # R4: an install that has not migrated keeps passing setup
        self.assertTrue(self.serves("%h/buzai", "buzai"))

    def test_an_absolute_path_to_this_checkout_serves_it(self):
        self.assertTrue(self.serves(str(self.home / "elsewhere"), "elsewhere"))

    def test_a_unit_serving_another_checkout_does_not(self):
        (self.home / "buzai").mkdir()
        self.assertFalse(self.serves("%h/buzai", "buzai-assistant"))

    def test_setup_refuses_with_the_fix_named(self):
        text = INSTALL_SH.read_text()
        step = text[text.index('step "8/9') : text.index('step "9/9')]
        self.assertIn("unit_serves_this_checkout", step)
        self.assertIn("make service-install", step)
        self.assertIn(MIGRATION, step)


if __name__ == "__main__":
    unittest.main()
