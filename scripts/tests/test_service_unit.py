"""The systemd unit template is a contract, and these tests hold it to it.

Four modules and the setup docs all state that the push backlog and the review
dispositions ref are "retried at service start", and that the remote's PRIVATE verdict is
"re-verified at service start". None of it was wired: the unit ran only
`secrets_preflight.py`, which is read-only and WARN-only. The real retry was the next
successful hub write or a hand-run `make hub-push`, and a cached PRIVATE verdict survived
a restart unrefreshed — so a repository flipped to public through a web UI, clone URL
unchanged, could be pushed to for an hour after a restart that was supposed to re-check.

A documented guarantee nothing implements is worse than no guarantee: it is the reason
nobody goes looking. So the wiring is asserted here, against the real template file, and
so is the thing a string match would miss — that the flags the unit passes are flags
those scripts actually accept.

Verified against the unfixed template: every test in
`TestTheServiceStartGuaranteesAreWired` failed, because the file contained exactly one
`ExecStartPre=` line and no `ExecStartPost=` line at all.
"""

import unittest
from pathlib import Path

from scripts.hub_commit import build_parser as commit_parser
from scripts.hub_remote import build_parser as remote_parser
from scripts.hub_review import build_parser as review_parser

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "deploy" / "claude-remote.service.template"
MAKEFILE = REPO_ROOT / "Makefile"

# The flag that turns "no hub store yet" from a failure into a note. It belongs to the
# unit alone: the same scripts run by hand as `make hub-push` / `make hub-remote-check`
# must fail on a missing store, or they report success on one that is not there.
SERVICE_FLAG = "--if-initialized"

# systemd's default. The start job now spans a preflight, a privacy probe and two
# pushes, so a unit that keeps the default can have a *running* assistant killed for
# being slow — which is the opposite of what these lines are for.
SYSTEMD_DEFAULT_START_TIMEOUT = 90


def make_recipe(target: str) -> list[str]:
    """The recipe lines of one Makefile target, stripped, in file order."""
    lines = MAKEFILE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    return recipe


def directives(name: str) -> list[str]:
    """Every value of `name=` in the unit, in file order, comments excluded."""
    return [
        line.split("=", 1)[1].strip()
        for line in TEMPLATE.read_text().splitlines()
        if line.strip().startswith(f"{name}=")
    ]


class TestTheServiceStartGuaranteesAreWired(unittest.TestCase):
    def setUp(self):
        self.pre = directives("ExecStartPre")
        self.post = directives("ExecStartPost")

    # --- the privacy verdict is refreshed, and before anything pushes --------------

    def test_the_preflight_still_runs_first_and_can_still_block_start(self):
        # the guard: the fatal leak gate must not have been softened by the new lines
        self.assertTrue(self.pre[0].endswith("scripts/secrets_preflight.py"), self.pre)
        self.assertFalse(self.pre[0].startswith("-"), "the leak gate must be able to fail start")

    def test_the_privacy_verdict_is_re_verified(self):
        self.assertTrue(
            any("scripts/hub_remote.py" in p for p in self.pre),
            f"nothing re-verifies the remote at start: {self.pre}",
        )

    def test_the_re_verification_cannot_block_start(self):
        refresh = next(p for p in self.pre if "scripts/hub_remote.py" in p)
        self.assertTrue(refresh.startswith("-"), "a 'do not push' answer must not end the service")

    def test_it_is_an_ExecStartPre_so_it_precedes_every_push(self):
        # ordering is the whole point: a push must never run against an unrefreshed verdict
        self.assertTrue(any("hub_remote.py" in p for p in self.pre))
        self.assertFalse(any("hub_remote.py" in p for p in self.post))

    # --- the backlogs are drained -------------------------------------------------

    def test_the_push_backlog_is_retried(self):
        self.assertTrue(
            any("hub_commit.py --retry-push" in p for p in self.post),
            f"nothing drains the push backlog at start: {self.post}",
        )

    def test_the_notes_ref_is_pushed(self):
        self.assertTrue(
            any("hub_review.py --push-notes" in p for p in self.post),
            f"nothing pushes the review dispositions at start: {self.post}",
        )

    def test_the_notes_push_is_chained_behind_the_commit_push(self):
        # `make hub-push` chains with && deliberately: a note must never reach the remote
        # ahead of the correction commit it refers to
        line = next(p for p in self.post if "--retry-push" in p)
        self.assertIn("--push-notes", line, "the two pushes must be one chained command")
        self.assertLess(line.index("--retry-push"), line.index("--push-notes"))
        self.assertIn("&&", line)

    def test_no_push_can_fail_the_unit(self):
        for line in self.post:
            self.assertTrue(line.startswith("-"), f"ExecStartPost without '-': {line}")

    # --- the start job has room for what it now does ------------------------------

    def test_the_start_timeout_is_raised_above_the_default(self):
        values = directives("TimeoutStartSec")
        self.assertEqual(len(values), 1, values)
        seconds = int(values[0].rstrip("s"))
        self.assertGreater(seconds, SYSTEMD_DEFAULT_START_TIMEOUT)


class TestOnlyTheUnitExcusesAMissingStore(unittest.TestCase):
    """c74c3d6 made these three scripts exit 0 on a missing hub store unconditionally, to
    keep `FAIL:` out of the journal at every start before `make hub-init`. But the Makefile
    runs the same commands for the owner, so `make hub-push` and `make hub-remote-check`
    reported success on a store that did not exist. The downgrade is now a flag, and these
    tests hold it to the unit and away from the Makefile."""

    def invocations(self, script: str) -> list[str]:
        """Each command in the unit that runs `script`, split on the `&&` chain."""
        lines = directives("ExecStartPre") + directives("ExecStartPost")
        return [part for line in lines for part in line.split("&&") if script in part]

    def arguments(self, command: str) -> list[str]:
        # tokens without the `sh -c '...'` quoting, which clings to the last one
        return [token.strip("'\"") for token in command.split()]

    def test_the_privacy_refresh_passes_the_flag(self):
        (refresh,) = self.invocations("scripts/hub_remote.py")
        self.assertIn(SERVICE_FLAG, self.arguments(refresh))

    def test_both_drains_pass_the_flag(self):
        for script in ("scripts/hub_commit.py", "scripts/hub_review.py"):
            (drain,) = self.invocations(script)
            self.assertIn(SERVICE_FLAG, self.arguments(drain), drain)

    def test_the_owner_run_targets_do_not(self):
        for target, script in (
            ("hub-push", "scripts/hub_commit.py --retry-push"),
            ("hub-push", "scripts/hub_review.py --push-notes"),
            ("hub-remote-check", "scripts/hub_remote.py"),
        ):
            recipe = " ".join(make_recipe(target))
            self.assertIn(script, recipe, target)  # the target still runs the script
            self.assertNotIn(SERVICE_FLAG, recipe, target)

    def test_every_script_accepts_the_flag(self):
        self.assertTrue(commit_parser().parse_args(["--retry-push", SERVICE_FLAG]).if_initialized)
        self.assertTrue(review_parser().parse_args(["--push-notes", SERVICE_FLAG]).if_initialized)
        self.assertTrue(remote_parser().parse_args([SERVICE_FLAG]).if_initialized)


class TestPrimingRunsTheModeTheServiceRuns(unittest.TestCase):
    """`make prime-consent` exists to answer prompts the headless service cannot. It ran
    `claude remote-control --name <NAME>` with no `--spawn`, so it stopped at a spawn-mode
    prompt the service never sees — the unit has always passed `--spawn=same-dir`, and
    SETUP said so while still telling the owner to answer the prompt by hand.

    Observed on a real install: the TUI reads that prompt in raw mode, so Ctrl-C arrives as
    a literal byte instead of SIGINT. The priming step wedged, survived `kill %1` (SIGTERM
    cannot be delivered to a job stopped by Ctrl-Z), and had to be SIGKILLed.

    The consent itself has no flag and is still answered by hand — that is the one thing
    this step is for.
    """

    SPAWN_FLAG = "--spawn=same-dir"

    def recipe(self) -> str:
        return " ".join(make_recipe("prime-consent"))

    def test_priming_passes_the_spawn_mode(self):
        self.assertIn(self.SPAWN_FLAG, self.recipe())

    def test_it_is_the_same_mode_the_unit_runs(self):
        # the point is parity: priming in one mode and serving in another would prime
        # the wrong project state
        (exec_start,) = directives("ExecStart")
        self.assertIn(self.SPAWN_FLAG, exec_start)

    def test_priming_still_runs_remote_control_under_the_configured_name(self):
        recipe = self.recipe()
        self.assertIn("claude remote-control", recipe)
        self.assertIn("--name", recipe)

    def test_no_doc_still_tells_the_owner_to_answer_the_spawn_prompt(self):
        # the instruction outlived the prompt once already; these are the three places
        # that carried it
        for path in (
            REPO_ROOT / "docs" / "SETUP.md",
            REPO_ROOT / "docs" / "QUICKSTART.md",
            REPO_ROOT / "docs" / "TROUBLESHOOTING.md",
            REPO_ROOT / "install.sh",
        ):
            text = path.read_text()
            self.assertNotIn("Spawn mode for this project", text, path.name)
            self.assertNotIn("Spawn mode 1", text, path.name)
            self.assertNotIn("spawn mode\n   **1**", text, path.name)


class TestTheUnitOnlyNamesThingsThatExist(unittest.TestCase):
    """A string match in the template proves nothing on its own — these calls have to be
    real. A renamed flag would otherwise leave the unit silently doing nothing again."""

    def commands(self) -> list[str]:
        return directives("ExecStartPre") + directives("ExecStartPost") + directives("ExecStart")

    def scripts_named(self) -> set[str]:
        found = set()
        for line in self.commands():
            for token in line.split():
                if token.endswith(".py"):
                    found.add(token.rsplit("/", 1)[-1])
        return found

    def test_every_script_the_unit_runs_exists(self):
        named = self.scripts_named()
        self.assertTrue(named)
        for name in named:
            self.assertTrue((REPO_ROOT / "scripts" / name).is_file(), name)

    def test_retry_push_is_a_flag_hub_commit_accepts(self):
        self.assertTrue(commit_parser().parse_args(["--retry-push"]).retry_push)

    def test_push_notes_is_a_flag_hub_review_accepts(self):
        self.assertTrue(review_parser().parse_args(["--push-notes"]).push_notes)

    def test_the_interpreter_is_the_pinned_venv_not_the_system_python(self):
        for line in self.commands():
            for token in line.split():
                if token.endswith(".py"):
                    self.assertIn("/buzai/.venv/bin/python", line, line)
                    break

    def test_every_path_is_rewritable_by_make_service_install(self):
        # `make service-install WORKDIR=…` runs `sed 's|%h/buzai|$(WORKDIR)|g'`, so a
        # checkout path written any other way silently keeps pointing at ~/buzai
        for line in self.commands():
            for token in line.split():
                if token.endswith(".py"):
                    self.assertIn("%h/buzai/", token)


if __name__ == "__main__":
    unittest.main()
