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

import os
import subprocess
import tempfile
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


def directives(name: str, section: str | None = None, unit: str | None = None) -> list[str]:
    """Every value of `name=` in the unit, in file order, comments excluded — or only
    those inside `[section]`, when given. `unit` is the unit's text; default, the template.

    systemd ignores a directive in the wrong section (with a warning), so a key that is
    present but misplaced does nothing; pass `section` wherever placement matters.
    systemd also accepts `Key = value`, so the key is matched with spaces stripped.
    Line continuations (a trailing backslash) are not joined: the template has none, and
    `test_the_template_has_no_line_continuations` holds it to that."""
    values, current = [], None
    for raw in (TEMPLATE.read_text() if unit is None else unit).splitlines():
        line = raw.strip()
        if line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip() == name and section in (None, current):
            values.append(value.strip())
    return values


class TestTheUnitNeverGivesUpButRetriesSlowly(unittest.TestCase):
    """`StartLimitBurst=5` in a 300s window turned an expired credential — which SETUP
    calls an expected steady state — into a unit left `failed` for good: five failed
    starts and systemd stopped trying, and it went on not trying after the credential was
    renewed, until someone restarted it by hand. A transient condition became a permanent,
    silent outage.

    Removing the limit alone is the opposite failure: at `RestartSec=10s` a token outage
    is ~360 failed starts an hour of journal noise. So the unit never gives up, and waits
    a minute between tries. Noticing a long outage is the watchdog's job (#4), not the
    start limit's — and the watchdog is not built yet, so until it is nothing alarms on a
    unit stuck in `activating (auto-restart)`.

    Verified against the unfixed template: all three tests failed — it carried
    `StartLimitIntervalSec=300`, `StartLimitBurst=5` and `RestartSec=10s`.
    """

    def test_the_start_limit_is_disabled_in_the_unit_section(self):
        # StartLimitIntervalSec belongs to [Unit]; under [Service] systemd ignores it
        self.assertEqual(directives("StartLimitIntervalSec", "Unit"), ["0"])
        self.assertEqual(directives("StartLimitIntervalSec", "Service"), [])

    def test_there_is_no_burst_to_trip(self):
        # with the interval at 0 a burst is inert, but a stray one invites "restoring" the
        # interval and quietly bringing the give-up back
        self.assertEqual(directives("StartLimitBurst"), [])

    def test_it_restarts_always_and_a_minute_apart(self):
        self.assertEqual(directives("Restart", "Service"), ["always"])
        self.assertEqual(directives("RestartSec", "Service"), ["60s"])


class TestTheDirectiveReaderReadsKeysAsSystemdDoes(unittest.TestCase):
    """The guards above are only as good as `directives()`, on spacing, sections and
    comments. systemd accepts `Key = value` and ignores keys in the wrong section; a
    reader that matched `Key=` by prefix let a spaced `StartLimitBurst = 5` straight past
    `test_there_is_no_burst_to_trip`. The real template has no spaced keys, so these feed
    the reader invented units."""

    UNIT = "[Unit]\n# StartLimitBurst=9\nStartLimitBurst = 5\n[Service]\nRestartSec=60s\n"

    def test_a_spaced_key_is_read(self):
        self.assertEqual(directives("StartLimitBurst", unit=self.UNIT), ["5"])

    def test_a_key_is_read_only_in_its_section(self):
        self.assertEqual(directives("StartLimitBurst", "Unit", self.UNIT), ["5"])
        self.assertEqual(directives("StartLimitBurst", "Service", self.UNIT), [])
        self.assertEqual(directives("RestartSec", "Service", self.UNIT), ["60s"])

    def test_a_commented_key_is_not_read(self):
        # systemd takes both '#' and ';' as comment markers, spaced or not
        for comment in ("#StartLimitBurst=5", "# StartLimitBurst = 5", ";StartLimitBurst = 5"):
            self.assertEqual(
                directives("StartLimitBurst", unit=f"[Unit]\n{comment}\n"), [], comment
            )

    def test_the_template_has_no_line_continuations(self):
        # the reader takes one physical line per directive; a wrapped one would be read
        # truncated, so the template must not wrap
        for number, line in enumerate(TEMPLATE.read_text().splitlines(), 1):
            if not line.lstrip().startswith(("#", ";")):
                self.assertFalse(line.rstrip().endswith("\\"), f"line {number}: {line}")


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


class TestTheUnitPinsThePermissionMode(unittest.TestCase):
    """The mode spawned sessions run in is pinned, not inherited.

    The account plan decides the default — auto on Pro/Max/Team, `default` (manual
    approval) elsewhere — so an unpinned unit gives different adopters different
    behaviour, and an always-on assistant that falls back to manual stalls on routine
    calls with nobody watching to approve them.

    Pinning does not weaken the gate, which is why `auto` is safe to pin: PreToolUse
    hooks run BEFORE any permission-mode check, in every mode, and a hook returning
    `deny` holds even under --dangerously-skip-permissions. The mode only decides what
    happens to calls the gate already allowed.
    """

    MODE_FLAG = "--permission-mode auto"

    def test_the_unit_pins_it(self):
        (exec_start,) = directives("ExecStart")
        self.assertIn(self.MODE_FLAG, exec_start)

    def test_priming_spawns_in_the_same_mode_it_will_serve_in(self):
        # priming that spawns under different rules primes the wrong project state
        self.assertIn(self.MODE_FLAG, " ".join(make_recipe("prime-consent")))

    def test_the_mode_is_one_claude_accepts(self):
        # `claude remote-control --help`: acceptEdits, auto, bypassPermissions,
        # default, dontAsk, plan
        mode = self.MODE_FLAG.split()[-1]
        self.assertIn(
            mode, {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
        )

    def test_the_unit_does_not_skip_permissions_wholesale(self):
        # the gate would still hold, but a unit that ships this is indefensible
        (exec_start,) = directives("ExecStart")
        self.assertNotIn("--dangerously-skip-permissions", exec_start)


class TestEveryVerbFindsTheToolsRegardlessOfShell(unittest.TestCase):
    """`claude` and `uv` live in ~/.local/bin, which only a LOGIN shell adds to PATH.

    `ssh host 'make auth'` runs a non-login shell and died with "claude: No such file or
    directory". `make setup` was unaffected, because its own phase exports the same path —
    which is exactly what made the failure look situational rather than structural.
    """

    def test_the_makefile_puts_local_bin_on_path(self):
        text = MAKEFILE.read_text()
        self.assertRegex(text, r"export PATH\s*:=\s*\$\(HOME\)/\.local/bin:\$\(PATH\)")

    def test_it_is_set_before_any_recipe_could_need_it(self):
        text = MAKEFILE.read_text()
        self.assertLess(text.index("export PATH"), text.index("auth:"))


class TestTheAssistantIsToldToUseThePinnedInterpreter(unittest.TestCase):
    """CLAUDE.md is the assistant's only instruction for writing to a hub, and it named
    `python3`. The hub scripts import `datetime.UTC`, which is 3.11+; Ubuntu 22.04 — the
    OS docs/SETUP.md targets — ships 3.10. So an assistant following the documented
    command got `ImportError: cannot import name 'UTC'` and could not write a hub at all,
    while `hub_paths.py` (which does not import UTC) ran fine under the same interpreter,
    making the failure look situational rather than systematic.

    Observed live on a fresh install. The Makefile's PY and the unit both use the pinned
    venv; only the instruction diverged, so that divergence is what this test pins.
    """

    PINNED = ".venv/bin/python"

    def instruction_files(self):
        for name in ("CLAUDE.md", "docs/SETUP.md", "docs/SMOKE-TEST.md", "docs/TROUBLESHOOTING.md"):
            path = REPO_ROOT / name
            if path.exists():
                yield path

    def test_no_instruction_file_tells_the_assistant_to_use_bare_python3(self):
        for path in self.instruction_files():
            self.assertNotIn("python3 scripts/", path.read_text(), path.name)

    def test_claude_md_shows_the_pinned_interpreter_for_hub_writes(self):
        text = (REPO_ROOT / "CLAUDE.md").read_text()
        self.assertIn(f"{self.PINNED} scripts/hub_commit.py", text)

    def test_the_makefile_uses_the_same_interpreter(self):
        # the instruction and the task runner must not drift apart again
        line = next(x for x in MAKEFILE.read_text().splitlines() if x.startswith("PY"))
        self.assertIn(self.PINNED, line)


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
                    self.assertIn("/buzai-assistant/.venv/bin/python", line, line)
                    break

    def test_every_path_is_rewritable_by_make_service_install(self):
        # `make service-install` rewrites the token `%h/buzai-assistant` to the checkout it
        # ran from, so a checkout path written any other way silently keeps pointing at
        # ~/buzai-assistant
        for line in self.commands():
            for token in line.split():
                if token.endswith(".py"):
                    self.assertIn("%h/buzai-assistant/", token)


# The suffixes that turn a unit path into the checkout it names.
CHECKOUT_SUFFIXES = ("/.venv/bin/python", "/scripts/")


def checkout_paths(unit: str, home: Path) -> set[str]:
    """Every checkout the unit names, with systemd's `%h` expanded to `home`: the
    WorkingDirectory, plus the directory each venv interpreter and script lives in."""
    found = {value.replace("%h", str(home)) for value in directives("WorkingDirectory", unit=unit)}
    for line in directives("ExecStartPre", unit=unit) + directives("ExecStartPost", unit=unit):
        for token in line.replace("'", " ").split():
            token = token.lstrip("-").replace("%h", str(home))
            for suffix in CHECKOUT_SUFFIXES:
                if suffix in token:
                    found.add(token.split(suffix)[0])
    return found


class TestServiceInstallPointsTheUnitAtItsCheckout(unittest.TestCase):
    """Issue #17. The Remote Control picker labels an environment by its working
    directory's basename, so a deployment at ~/buzai and a dev checkout of the repo both
    show as `buzai · <host>`. The deployment now lives at ~/buzai-assistant.

    The unit follows the checkout `make service-install` runs in, not a fixed default.
    A default of ~/buzai-assistant would point an existing ~/buzai install, re-running
    the verb after `git pull`, at a directory that does not exist.

    These run the real recipe in a scratch HOME, with a stub `systemctl` first on PATH
    (the Makefile's own PATH export puts $(HOME)/.local/bin there). Nothing is written
    outside the scratch directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        bin_dir = self.home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        self.calls = Path(self._tmp.name) / "systemctl.calls"
        stub = bin_dir / "systemctl"
        stub.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\n')
        stub.chmod(0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def checkout(self, name: str) -> Path:
        """A minimal checkout: the real Makefile and the real template, nothing else."""
        root = self.home / name
        (root / "deploy").mkdir(parents=True)
        (root / "Makefile").write_text(MAKEFILE.read_text())
        (root / "deploy" / TEMPLATE.name).write_text(TEMPLATE.read_text())
        return root

    def install(self, root: Path, *overrides: str) -> tuple[str, str]:
        # an inherited WORKDIR/NAME or make flags would change what the recipe does
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("MAKE") and k not in ("WORKDIR", "NAME")
        }
        env["HOME"] = str(self.home)
        run = subprocess.run(
            ["make", "--no-print-directory", "-C", str(root), "service-install", *overrides],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        unit = self.home / ".config" / "systemd" / "user" / "claude-remote.service"
        return unit.read_text(), run.stdout

    def test_a_default_checkout_gets_a_unit_naming_it(self):
        root = self.checkout("buzai-assistant")
        unit, _ = self.install(root)
        self.assertEqual(checkout_paths(unit, self.home), {str(root)})

    def test_an_old_buzai_checkout_keeps_a_unit_naming_it(self):
        # R4: an existing install that pulls and re-installs keeps working
        root = self.checkout("buzai")
        unit, _ = self.install(root)
        self.assertEqual(checkout_paths(unit, self.home), {str(root)})

    def test_an_explicit_workdir_replaces_the_whole_token(self):
        # the old pattern `%h/buzai` is a prefix of `%h/buzai-assistant`; matching it
        # would leave `<WORKDIR>-assistant` in every path
        root = self.checkout("buzai-assistant")
        elsewhere = Path(self._tmp.name) / "elsewhere"
        unit, _ = self.install(root, f"WORKDIR={elsewhere}")
        self.assertEqual(checkout_paths(unit, self.home), {str(elsewhere)})
        self.assertNotIn(f"{elsewhere}-assistant", unit)

    def test_a_checkout_named_buzai_is_told_about_the_picker(self):
        root = self.checkout("buzai")
        _, out = self.install(root)
        self.assertIn("picker", out)
        self.assertIn("Moving an existing install", out)

    def test_a_checkout_named_buzai_assistant_is_not(self):
        _, out = self.install(self.checkout("buzai-assistant"))
        self.assertNotIn("picker", out)

    def test_systemd_is_reloaded_through_the_stub(self):
        self.install(self.checkout("buzai-assistant"))
        self.assertIn("--user daemon-reload", self.calls.read_text())


if __name__ == "__main__":
    unittest.main()
