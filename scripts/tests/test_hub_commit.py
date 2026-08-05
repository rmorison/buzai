import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.hub_commit import (
    APPEND_ENTRY,
    AUTONOMOUS,
    COMMITTED,
    DIVERGED,
    FAILED,
    OWNER_CORRECTION,
    PUSH_FAILED,
    PUSH_LOCAL_ONLY,
    PUSH_REFUSED,
    PUSH_SKIPPED,
    PUSHED,
    REFUSED,
    REMOVE_ENTRY,
    REPLACE_SECTION,
    Backlog,
    HubCommitError,
    HubLock,
    LockBusy,
    Operation,
    WriteRequest,
    added_lines,
    apply_operation,
    commit_message,
    default_backlog_path,
    default_lock_path,
    entropy,
    main,
    parse_porcelain,
    read_backlog,
    record,
    record_unpushed,
    retry_push,
    scan_text,
)
from scripts.hub_remote import INDETERMINATE, LOCAL_ONLY, PRIVATE, GitResult, Verification
from scripts.hub_remote import default_git_runner as real_git

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)

# Hermetic git: no global/system config reaches these tests (a machine with none must
# pass, and a maintainer's gpgsign/hooks/defaultBranch settings must not leak in).
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "buzai tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "buzai tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}
_SAVED: dict[str, str | None] = {}


def setUpModule():
    for key, value in GIT_ENV.items():
        _SAVED[key] = os.environ.get(key)
        os.environ[key] = value


def tearDownModule():
    for key, value in _SAVED.items():
        restore_env(key, value)


def restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def git(*args, check=True) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, check=check)
    return r.stdout


def reap(proc: subprocess.Popen) -> None:
    """Kill, wait, and close — so a lock holder never outlives its test."""
    proc.kill()
    proc.wait(timeout=10)
    if proc.stdout:
        proc.stdout.close()


# --- injected side effects ---------------------------------------------------------
#
# Verification is always injected: the real one probes the network, and no test here is
# ever allowed to leave the box.


def verifier_for(verdict=PRIVATE, remote="origin", violations=()):
    detail = f"{verdict} (injected by the test)"
    verification = Verification(verdict, remote, "ssh://host.invalid/hubs.git", detail, "probe")
    if violations:
        verification = Verification(
            verdict, remote, "ssh://host.invalid/hubs.git", detail, "probe", tuple(violations)
        )
    return lambda hub: verification


PRIVATE_REMOTE = verifier_for(PRIVATE)
NO_REMOTE = verifier_for(LOCAL_ONLY, remote=None)
UNVERIFIED_REMOTE = verifier_for(INDETERMINATE)


def failing_git(on: str, message: str = "fatal: injected failure"):
    """A runner that fails one git subcommand and passes everything else through."""

    def runner(args, env, timeout):
        if on in args:
            return GitResult(1, "", message)
        return real_git(args, env, timeout)

    return runner


def hanging_git(on: str, delay: float):
    """A runner that blocks inside one subcommand — a blackholed network, in effect."""

    def runner(args, env, timeout):
        if on in args:
            time.sleep(delay)
            return GitResult(124, "", f"timed out after {timeout}s", timed_out=True)
        return real_git(args, env, timeout)

    return runner


DRIVER = """
import sys, time
sys.path.insert(0, {repo!r})
from datetime import UTC, datetime
from pathlib import Path
from scripts.hub_commit import APPEND_ENTRY, Operation, WriteRequest, record

hub, fact, start_at = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
while time.time() < start_at:
    time.sleep(0.002)
result = record(
    hub,
    WriteRequest(
        Operation(APPEND_ENTRY, "notes.md", f"- {{fact}}"),
        summary=f"Record {{fact}}",
        reason="a concurrent session recorded it",
    ),
    now=datetime.now(UTC),
    push=False,
)
print(result.status)
sys.exit(0 if result.status == "committed" else 1)
"""

HOLDER = """
import fcntl, sys, time
fh = open(sys.argv[1], "a+")
fcntl.flock(fh, fcntl.LOCK_EX)
fh.write("%d holder\\n" % __import__("os").getpid())
fh.flush()
print("held", flush=True)
time.sleep(float(sys.argv[2]))
"""


class HubRepoCase(unittest.TestCase):
    """A real hub repo in a tempdir. Never the network, never the real ~/hubs."""

    SEED = "# Notes\n\n- seeded fact\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hub = self.root / "hubs"
        self.hub.mkdir()
        git("init", "-q", "-b", "main", str(self.hub))
        self.notes = self.hub / "notes.md"
        self.notes.write_text(self.SEED)
        git("-C", str(self.hub), "add", "-A")
        git("-C", str(self.hub), "commit", "-q", "-m", "seed")

    def tearDown(self):
        self._tmp.cleanup()

    def commits(self) -> int:
        return int(git("-C", str(self.hub), "rev-list", "--count", "HEAD").strip())

    def log(self, fmt: str, n: int = 1) -> str:
        return git("-C", str(self.hub), "log", f"-{n}", f"--pretty={fmt}")

    def porcelain(self) -> str:
        return git("-C", str(self.hub), "status", "--porcelain")

    def bare_remote(self) -> Path:
        remote = self.root / "remote.git"
        git("init", "-q", "--bare", "-b", "main", str(remote))
        return remote

    def append(self, fact: str, **kw):
        kw.setdefault("push", False)
        return record(
            self.hub,
            WriteRequest(
                Operation(APPEND_ENTRY, "notes.md", f"- {fact}"),
                summary=f"Record {fact}",
                reason="a session recorded it",
                source=AUTONOMOUS,
            ),
            now=NOW,
            **kw,
        )


# --- pure core ---------------------------------------------------------------------


class TestApplyOperation(unittest.TestCase):
    DOC = "# Home\n\n## Vehicles\n\n- blue car\n\n## Pets\n\n- a dog\n"

    def test_append_without_a_section_goes_to_the_end(self):
        self.assertEqual(
            apply_operation(self.DOC, Operation(APPEND_ENTRY, "home.md", "- a note")),
            self.DOC + "- a note\n",  # one list, so no blank line between the items
        )

    def test_a_paragraph_entry_is_separated_by_a_blank_line(self):
        out = apply_operation(self.DOC, Operation(APPEND_ENTRY, "home.md", "A prose note."))
        self.assertEqual(out, self.DOC + "\nA prose note.\n")

    def test_append_under_a_section_lands_inside_it(self):
        out = apply_operation(
            self.DOC, Operation(APPEND_ENTRY, "home.md", "- red bike", section="Vehicles")
        )
        self.assertEqual(
            out, "# Home\n\n## Vehicles\n\n- blue car\n- red bike\n\n## Pets\n\n- a dog\n"
        )

    def test_append_to_a_missing_section_creates_it(self):
        out = apply_operation(
            self.DOC, Operation(APPEND_ENTRY, "home.md", "- a fact", section="Insurance")
        )
        self.assertTrue(out.endswith("## Insurance\n\n- a fact\n"))
        self.assertIn("- a dog", out)

    def test_append_to_an_empty_file_starts_it(self):
        self.assertEqual(
            apply_operation("", Operation(APPEND_ENTRY, "new.md", "- first")), "- first\n"
        )

    def test_append_is_case_insensitive_about_the_heading(self):
        out = apply_operation(
            self.DOC, Operation(APPEND_ENTRY, "home.md", "- red bike", section="vehicles")
        )
        self.assertIn("- blue car\n- red bike", out)

    def test_replace_section_keeps_the_rest_of_the_file(self):
        out = apply_operation(
            self.DOC, Operation(REPLACE_SECTION, "home.md", "- green car", section="Vehicles")
        )
        self.assertEqual(out, "# Home\n\n## Vehicles\n\n- green car\n\n## Pets\n\n- a dog\n")

    def test_replace_of_a_missing_section_appends_it(self):
        out = apply_operation(
            self.DOC, Operation(REPLACE_SECTION, "home.md", "- none", section="Boats")
        )
        self.assertTrue(out.endswith("## Boats\n\n- none\n"))

    def test_replace_without_a_section_is_an_error(self):
        with self.assertRaises(HubCommitError):
            apply_operation(self.DOC, Operation(REPLACE_SECTION, "home.md", "x"))

    def test_remove_entry_takes_the_blank_line_with_it(self):
        out = apply_operation(self.DOC, Operation(REMOVE_ENTRY, "home.md", "- a dog"))
        self.assertEqual(out, "# Home\n\n## Vehicles\n\n- blue car\n\n## Pets\n")

    def test_remove_entry_can_be_scoped_to_a_section(self):
        doc = "## A\n\n- same line\n\n## B\n\n- same line\n"
        out = apply_operation(doc, Operation(REMOVE_ENTRY, "h.md", "- same line", section="B"))
        self.assertEqual(out, "## A\n\n- same line\n\n## B\n")

    def test_remove_of_a_missing_entry_fails_loudly(self):
        # a silent no-op would put a commit message in history describing a change that
        # never happened — the exact failure R5 cannot tolerate
        with self.assertRaises(HubCommitError) as cm:
            apply_operation(self.DOC, Operation(REMOVE_ENTRY, "home.md", "- a cat"))
        self.assertIn("nothing was removed", str(cm.exception))

    def test_multi_line_entries_round_trip(self):
        entry = "- a fact\n  with a continuation"
        out = apply_operation(self.DOC, Operation(APPEND_ENTRY, "home.md", entry))
        self.assertEqual(apply_operation(out, Operation(REMOVE_ENTRY, "home.md", entry)), self.DOC)

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(HubCommitError):
            apply_operation(self.DOC, Operation("rewrite-everything", "home.md", "x"))

    def test_empty_text_is_refused(self):
        for kind in (APPEND_ENTRY, REMOVE_ENTRY):
            with self.assertRaises(HubCommitError):
                apply_operation(self.DOC, Operation(kind, "home.md", "  \n "))


class TestAddedLines(unittest.TestCase):
    def test_only_new_lines_are_reported(self):
        self.assertEqual(added_lines("a\nb\n", "a\nb\nc\n"), [(3, "c")])

    def test_pre_existing_lines_are_never_re_reported(self):
        # otherwise one pre-existing false positive would refuse every future write
        self.assertEqual(added_lines("keep\n", "keep\nkeep\n"), [])


class TestCommitMessage(unittest.TestCase):
    MESSAGE = commit_message(
        WriteRequest(
            Operation(APPEND_ENTRY, "home.md", "- the boiler was serviced", section="House"),
            summary="Record the boiler service",
            reason="the owner mentioned it in passing during a scheduling turn",
            source=OWNER_CORRECTION,
        )
    )

    def test_subject_states_the_change(self):
        self.assertEqual(self.MESSAGE.splitlines()[0], "Record the boiler service")

    def test_body_is_reviewable_without_the_diff(self):
        self.assertIn("appended a 1-line entry to home.md under 'House'", self.MESSAGE)
        self.assertIn("Why: the owner mentioned it in passing", self.MESSAGE)

    def test_trailers_name_the_source_and_the_file(self):
        self.assertIn("instruction-source: owner-correction", self.MESSAGE)
        self.assertIn("hub-file: home.md", self.MESSAGE)
        self.assertIn("hub-operation: append-entry", self.MESSAGE)

    def test_missing_summary_falls_back_to_a_description(self):
        message = commit_message(
            WriteRequest(Operation(REMOVE_ENTRY, "a.md", "- x"), summary="", reason="")
        )
        self.assertTrue(message.startswith("removed a 1-line entry from a.md"))


class TestParsePorcelain(unittest.TestCase):
    def test_plain_paths(self):
        self.assertEqual(parse_porcelain(" M a.md\0?? b.md\0"), ["a.md", "b.md"])

    def test_rename_records_carry_both_sides(self):
        self.assertEqual(parse_porcelain("R  new.md\0old.md\0"), ["old.md", "new.md"])

    def test_empty_is_clean(self):
        self.assertEqual(parse_porcelain(""), [])


# --- the credential scan ------------------------------------------------------------


class TestCredentialScan(unittest.TestCase):
    LEGITIMATE = """# Finance and tax

- Accountant: Dana Ruiz, dana@ruiz-accounting.example.com, +1 555 0142.
- Portal: https://portal.hmrc.gov.example/self-assessment/2026 (handle @rmorison).
- The 2025 return was filed on 2026-01-28; reference UTR 1234567890.
- Server box-01.lan (192.168.1.24) holds the scanned receipts.
- Password for the receipts share: see the 1Password entry "Receipts".
- Related commit 8f14e45fceea167a5a36dedd4bea2543c2f21fd6 in the notes repo.
- Session id 3f2504e0-4f89-11d3-9a0c-0305e82c3301 from the migration.
- Photo: https://photos.example.com/albums/2026/summer-holiday-in-the-lakes/index.html
"""

    def test_legitimate_hub_content_is_not_flagged(self):
        # the whole reason publish_gate's PII patterns are not reused: hostnames,
        # handles, emails and account numbers are what a personal hub is FOR
        self.assertEqual(scan_text(self.LEGITIMATE), [])

    def test_github_token_is_caught(self):
        findings = scan_text("- token: ghp_" + "A1b2C3d4E5f6G7h8I9j0" + "K1l2M3n4O5p6Q7r8S9t0")
        self.assertIn("github personal access token", [f.rule for f in findings])

    def test_fine_grained_github_token_is_caught(self):
        token = "github_pat_11ABCDEFG0" + "abcdefghijklmnopqrstuvwxyz012345"
        self.assertTrue(scan_text(f"- the deploy key is {token}"))

    def test_private_key_header_is_caught(self):
        block = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAA\n"
        self.assertIn("private key block", [f.rule for f in scan_text(block)])

    def test_authorization_header_value_is_caught(self):
        line = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K"
        rules = [f.rule for f in scan_text(line)]
        self.assertTrue({"Authorization header value", "json web token"} & set(rules))

    def test_aws_key_id_is_caught(self):
        self.assertIn("aws access key id", [f.rule for f in scan_text("AKIAIOSFODNN7EXAMPLE")])

    def test_secret_shaped_assignment_is_caught(self):
        line = 'api_key = "8Xj2Qm4Zp7Rw1Nc6Vb0Ty5"'
        self.assertIn("secret-shaped assignment", [f.rule for f in scan_text(line)])

    def test_placeholder_assignments_are_not_flagged(self):
        for line in (
            "password: <your-password-here>",
            "api_key = ${HUB_API_KEY}",
            "token: xxxxxxxxxxxx",
        ):
            self.assertEqual(scan_text(line), [], line)

    def test_high_entropy_blob_is_caught(self):
        blob = "Zk3Qm7Xr2Bv9Nc4Ty8Lw1Ps6Hd5Gf0Ja"
        self.assertIn("high-entropy opaque string", [f.rule for f in scan_text(blob)])

    def test_findings_never_carry_the_value(self):
        secret = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        for finding in scan_text(f"- {secret}"):
            self.assertNotIn(secret, str(finding))
            self.assertIn("line 1", str(finding))

    def test_entropy_ranks_random_above_prose(self):
        self.assertGreater(entropy("Zk3Qm7Xr2Bv9Nc4Ty8Lw1Ps6Hd5Gf0Ja"), entropy("aaaaaaaaaaaaaaaa"))


# --- the lock ------------------------------------------------------------------------


class TestHubLock(HubRepoCase):
    def lock_path(self) -> Path:
        return default_lock_path(self.hub)

    def start_holder(self, seconds: float) -> subprocess.Popen:
        script = self.root / "holder.py"
        script.write_text(HOLDER)
        path = self.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, str(script), str(path), str(seconds)],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(reap, proc)
        self.assertEqual(proc.stdout.readline().strip(), "held")  # it really holds it now
        return proc

    def test_clean_release_leaves_no_record(self):
        with HubLock(self.lock_path()) as lock:
            self.assertIsNone(lock.recovered_pid)
            self.assertIn(str(os.getpid()), self.lock_path().read_text())
        self.assertEqual(self.lock_path().read_text(), "")

    def test_stale_record_from_a_dead_process_is_cleared(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        path = self.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{dead.pid} 2026-08-05T11:00:00+00:00\n")

        result = self.append("fact-after-a-crash")

        self.assertEqual(result.status, COMMITTED)
        self.assertIn(str(dead.pid), result.lock_note)
        self.assertEqual(path.read_text(), "")

    def test_a_killed_holder_releases_the_lock_immediately(self):
        # flock is released by the kernel on process death, which is what makes an
        # OOM-killed session unable to wedge every future write on the box
        holder = self.start_holder(60)
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)

        started = time.monotonic()
        result = self.append("fact-after-a-kill")

        self.assertEqual(result.status, COMMITTED)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_live_holder_makes_the_writer_queue_not_fail(self):
        self.start_holder(1.0)
        started = time.monotonic()

        result = self.append("fact-after-queueing", lock_timeout=20.0)

        elapsed = time.monotonic() - started
        self.assertEqual(result.status, COMMITTED)
        self.assertGreater(elapsed, 0.5, "it should have waited for the holder")

    def test_a_holder_past_the_timeout_fails_without_writing(self):
        self.start_holder(30)

        result = self.append("never-written", lock_timeout=0.3)

        self.assertEqual(result.status, FAILED)
        self.assertIn("held the lock", result.detail)
        self.assertEqual(self.notes.read_text(), self.SEED)
        self.assertEqual(self.commits(), 1)

    def test_lock_busy_is_a_hub_commit_error(self):
        self.start_holder(30)
        with self.assertRaises(LockBusy):
            with HubLock(self.lock_path(), timeout=0.2):
                pass


# --- the write ------------------------------------------------------------------------


class TestHappyPath(HubRepoCase):
    def setUp(self):
        super().setUp()
        self.result = self.append("the boiler was serviced")

    def test_one_write_is_one_commit(self):
        self.assertEqual(self.result.status, COMMITTED)
        self.assertTrue(self.result.recorded)
        self.assertEqual(self.commits(), 2)

    def test_the_content_is_in_the_file_and_the_tree_is_clean(self):
        self.assertIn("- the boiler was serviced", self.notes.read_text())
        self.assertEqual(self.porcelain(), "")

    def test_the_message_states_what_changed_and_why(self):
        body = self.log("%B")
        self.assertIn("Record the boiler was serviced", body)
        self.assertIn("appended a 1-line entry to notes.md", body)
        self.assertIn("Why: a session recorded it", body)

    def test_the_trailer_names_the_instruction_source(self):
        self.assertIn("instruction-source: autonomous", self.log("%B"))

    def test_the_author_is_the_assistant_not_the_owner(self):
        self.assertEqual(self.log("%an").strip(), "buzai assistant")
        self.assertEqual(self.log("%cn").strip(), "buzai assistant")

    def test_global_git_config_is_left_alone(self):
        # the identity is per-invocation; nothing is written to the repo's config either
        config = git("-C", str(self.hub), "config", "--local", "--list")
        self.assertNotIn("buzai assistant", config)

    def test_push_was_not_attempted(self):
        self.assertEqual(self.result.push.status, PUSH_SKIPPED)


class TestWriteRefusals(HubRepoCase):
    def test_an_operation_that_changes_nothing_fails(self):
        result = record(
            self.hub,
            WriteRequest(
                Operation(REMOVE_ENTRY, "notes.md", "- never present"), "Remove it", "asked to"
            ),
            now=NOW,
            push=False,
        )
        self.assertEqual(result.status, FAILED)
        self.assertIn("nothing was removed", result.detail)
        self.assertEqual(self.commits(), 1)

    def test_a_path_outside_the_hub_is_refused(self):
        result = record(
            self.hub,
            WriteRequest(Operation(APPEND_ENTRY, "../escape.md", "- x"), "Escape", "why not"),
            now=NOW,
            push=False,
        )
        self.assertEqual(result.status, FAILED)
        self.assertFalse((self.root / "escape.md").exists())

    def test_a_path_inside_the_git_directory_is_refused(self):
        result = record(
            self.hub,
            WriteRequest(Operation(APPEND_ENTRY, ".git/notes.md", "- x"), "Sneak", "why not"),
            now=NOW,
            push=False,
        )
        self.assertEqual(result.status, FAILED)
        self.assertIn("git directory", result.detail)

    def test_an_unknown_instruction_source_is_refused(self):
        result = record(
            self.hub,
            WriteRequest(Operation(APPEND_ENTRY, "notes.md", "- x"), "s", "r", source="whatever"),
            now=NOW,
            push=False,
        )
        self.assertEqual(result.status, FAILED)
        self.assertEqual(self.commits(), 1)


class TestCredentialRefusal(HubRepoCase):
    TOKEN = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

    def test_a_planted_token_is_refused_and_the_file_rolled_back(self):
        result = self.append(f"the deploy token is {self.TOKEN}")

        self.assertEqual(result.status, REFUSED)
        self.assertEqual(self.notes.read_text(), self.SEED)  # rolled back, byte for byte
        self.assertEqual(self.commits(), 1)
        self.assertEqual(self.porcelain(), "")  # and nothing left dirty on disk
        self.assertIn("github personal access token", result.detail)
        self.assertNotIn(self.TOKEN, result.detail)  # the refusal never echoes the secret

    def test_a_refused_write_to_a_new_file_leaves_no_file_behind(self):
        result = record(
            self.hub,
            WriteRequest(
                Operation(APPEND_ENTRY, "secrets.md", f"- {self.TOKEN}"), "Record it", "asked"
            ),
            now=NOW,
            push=False,
        )
        self.assertEqual(result.status, REFUSED)
        self.assertFalse((self.hub / "secrets.md").exists())

    def test_a_private_key_body_is_refused(self):
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU\n"
        self.assertEqual(self.append(key).status, REFUSED)
        self.assertEqual(self.commits(), 1)

    def test_ordinary_content_still_commits(self):
        self.assertEqual(self.append("the accountant is dana@ruiz.example.com").status, COMMITTED)


class TestCommitFailure(HubRepoCase):
    def test_a_failed_commit_is_surfaced_and_the_file_rolled_back(self):
        result = self.append("a fact", runner=failing_git("commit", "fatal: disk full"))

        self.assertEqual(result.status, FAILED)
        self.assertIn("git commit failed", result.detail)
        self.assertIn("disk full", result.detail)
        self.assertEqual(self.notes.read_text(), self.SEED)
        self.assertEqual(self.commits(), 1)

    def test_a_failed_add_is_surfaced(self):
        result = self.append("a fact", runner=failing_git("add"))
        self.assertEqual(result.status, FAILED)
        self.assertIn("git add failed", result.detail)

    def test_an_unreadable_repo_is_surfaced_not_ignored(self):
        result = self.append("a fact", runner=failing_git("status"))
        self.assertEqual(result.status, FAILED)
        self.assertIn("working tree state", result.detail)

    def test_no_git_identity_configured_still_commits(self):
        # the assistant identity is supplied per invocation, so an instance whose owner
        # never ran `git config --global user.name` can still record knowledge
        for key in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        ):
            self.addCleanup(restore_env, key, os.environ.get(key))
            os.environ.pop(key, None)

        result = self.append("a fact with no configured identity")

        self.assertEqual(result.status, COMMITTED)
        self.assertEqual(self.log("%an").strip(), "buzai assistant")


class TestDirtyTreeReconciliation(HubRepoCase):
    def test_pre_existing_changes_are_committed_as_unattributed(self):
        (self.hub / "home.md").write_text("# Home\n\n- typed straight into the file\n")
        self.notes.write_text(self.SEED + "- edited by hand\n")

        result = self.append("a fact from the assistant")

        self.assertEqual(result.status, COMMITTED)
        self.assertEqual(self.commits(), 3)  # seed + reconcile + ours
        self.assertEqual(sorted(result.reconciled.committed), ["home.md", "notes.md"])
        reconcile = git("-C", str(self.hub), "log", "-1", "--skip=1", "--pretty=%B%n%an")
        self.assertIn("instruction-source: unattributed", reconcile)
        self.assertIn("buzai unattributed", reconcile)
        self.assertIn("home.md", reconcile)

    def test_the_owners_edit_is_not_attributed_to_the_assistant(self):
        self.notes.write_text(self.SEED + "- edited by hand\n")
        self.append("a fact from the assistant")
        authors = git("-C", str(self.hub), "log", "--pretty=%an").split("\n")
        self.assertEqual(authors[0], "buzai assistant")
        self.assertEqual(authors[1], "buzai unattributed")

    def test_everything_ends_up_committed_and_the_tree_is_clean(self):
        (self.hub / "home.md").write_text("# Home\n")
        self.append("a fact")
        self.assertEqual(self.porcelain(), "")

    def test_a_clean_tree_produces_no_reconcile_commit(self):
        result = self.append("a fact")
        self.assertIsNone(result.reconciled.commit)
        self.assertEqual(self.commits(), 2)

    def test_credential_shaped_dirty_content_is_left_dirty_and_reported(self):
        (self.hub / "leaked.md").write_text("token ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\n")

        result = self.append("an unrelated fact")

        self.assertEqual(result.status, COMMITTED)  # the assistant's own write still lands
        self.assertEqual([p for p, _ in result.reconciled.skipped], ["leaked.md"])
        self.assertIn("leaked.md", self.porcelain())
        self.assertNotIn("leaked.md", git("-C", str(self.hub), "ls-files"))


# --- concurrency ----------------------------------------------------------------------


class TestConcurrentWriters(HubRepoCase):
    """The unit's reason to exist: two sessions updating the SAME file must not clobber
    each other. A commit-count assertion passes against an implementation that silently
    loses an update — the assertion that matters is on the final FILE CONTENT. Verified
    against a naive version (lock around the commit only): every writer reported success
    while two of six facts vanished."""

    FACTS = [f"fact-{n}" for n in "ABCDEF"]

    def test_threaded_writers_all_survive_in_the_file(self):
        results = {}
        barrier = threading.Barrier(len(self.FACTS))

        def writer(fact):
            barrier.wait()  # every read starts before any write finishes
            results[fact] = self.append(fact)

        threads = [threading.Thread(target=writer, args=(f,)) for f in self.FACTS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        content = self.notes.read_text()
        missing = [f for f in self.FACTS if f not in content]
        self.assertEqual(missing, [], f"lost updates: {missing}\n---\n{content}")
        self.assertEqual([r.status for r in results.values()], [COMMITTED] * len(self.FACTS))
        self.assertEqual(self.commits(), 1 + len(self.FACTS))

    def test_subprocess_writers_all_survive_in_the_file(self):
        driver = self.root / "driver.py"
        driver.write_text(DRIVER.format(repo=str(REPO_ROOT)))
        facts = self.FACTS[:4]
        # a common wall-clock start, so the reads really overlap rather than being
        # serialized by interpreter startup
        start_at = time.time() + 1.5
        procs = [
            subprocess.Popen(
                [sys.executable, str(driver), str(self.hub), fact, str(start_at)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for fact in facts
        ]
        outs = [(p.wait(timeout=60), p.communicate()) for p in procs]

        content = self.notes.read_text()
        missing = [f for f in facts if f not in content]
        self.assertEqual(missing, [], f"lost updates: {missing}\n---\n{content}\n{outs}")
        self.assertEqual([rc for rc, _ in outs], [0] * len(facts), outs)
        self.assertEqual(self.commits(), 1 + len(facts))


class TestHungPushDoesNotBlockWrites(HubRepoCase):
    def test_a_second_session_commits_while_a_push_hangs(self):
        hang = 6.0
        finished = threading.Event()

        def hanging_writer():
            self.append(
                "recorded while the network is blackholed",
                push=True,
                runner=hanging_git("push", hang),
                verifier=PRIVATE_REMOTE,
                push_timeout=hang * 2,
            )
            finished.set()

        thread = threading.Thread(target=hanging_writer, daemon=True)
        thread.start()
        time.sleep(1.0)  # the first writer has committed and is now stuck in its push
        self.assertFalse(finished.is_set())

        started = time.monotonic()
        second = self.append("recorded by a second session", lock_timeout=5.0)
        elapsed = time.monotonic() - started

        self.assertEqual(second.status, COMMITTED)
        self.assertLess(elapsed, 4.0, "the hung push must not be holding the write lock")
        thread.join(timeout=hang * 2)


# --- push, backlog, divergence ---------------------------------------------------------


class TestPushBacklog(HubRepoCase):
    def test_unreachable_remote_still_records_and_backlogs_it(self):
        """Covers AE3: the write succeeds, the commit is local, the backlog remembers."""
        git("-C", str(self.hub), "remote", "add", "origin", str(self.root / "nowhere.git"))

        result = self.append("a fact recorded while offline", push=True, verifier=PRIVATE_REMOTE)

        self.assertEqual(result.status, COMMITTED)
        self.assertTrue(result.recorded)
        self.assertEqual(result.push.status, PUSH_FAILED)
        self.assertEqual(self.commits(), 2)
        backlog = read_backlog(default_backlog_path(self.hub))
        self.assertEqual(backlog.count, 1)
        self.assertEqual(backlog.pending[0][0], result.commit)
        self.assertEqual(backlog.oldest_age_seconds(NOW + timedelta(hours=2)), 7200.0)

    def test_a_later_retry_pushes_the_backlog(self):
        remote = self.bare_remote()
        git("-C", str(self.hub), "remote", "add", "origin", str(self.root / "nowhere.git"))
        result = self.append("a fact recorded while offline", push=True, verifier=PRIVATE_REMOTE)
        git("-C", str(self.hub), "remote", "set-url", "origin", str(remote))

        outcome = retry_push(self.hub, verifier=PRIVATE_REMOTE, now=NOW)

        self.assertEqual(outcome.status, PUSHED)
        self.assertEqual(outcome.backlog.count, 0)
        self.assertIn(result.commit, git("-C", str(remote), "rev-parse", "refs/heads/main"))

    def test_retry_with_an_empty_backlog_does_nothing(self):
        outcome = retry_push(self.hub, verifier=PRIVATE_REMOTE, now=NOW)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.backlog.count, 0)

    def test_a_successful_push_reports_and_clears(self):
        remote = self.bare_remote()
        git("-C", str(self.hub), "remote", "add", "origin", str(remote))

        result = self.append("a fact", push=True, verifier=PRIVATE_REMOTE)

        self.assertEqual(result.push.status, PUSHED)
        self.assertEqual(result.push.backlog.count, 0)
        self.assertIn(result.commit, git("-C", str(remote), "rev-parse", "refs/heads/main"))

    def test_an_unverified_remote_is_refused_and_backlogged_never_pushed(self):
        remote = self.bare_remote()
        git("-C", str(self.hub), "remote", "add", "origin", str(remote))

        result = self.append("a fact", push=True, verifier=UNVERIFIED_REMOTE)

        self.assertEqual(result.status, COMMITTED)
        self.assertEqual(result.push.status, PUSH_REFUSED)
        self.assertEqual(result.push.backlog.count, 1)
        # the decisive assertion: nothing reached a remote that was not proven private
        self.assertEqual(git("-C", str(remote), "branch", "--list").strip(), "")

    def test_a_credential_violation_blocks_the_push_even_when_private(self):
        remote = self.bare_remote()
        git("-C", str(self.hub), "remote", "add", "origin", str(remote))
        verifier = verifier_for(PRIVATE, violations=("the URL embeds a credential",))

        result = self.append("a fact", push=True, verifier=verifier)

        self.assertEqual(result.push.status, PUSH_REFUSED)
        self.assertEqual(git("-C", str(remote), "branch", "--list").strip(), "")

    def test_no_remote_is_reported_as_local_only_not_as_a_failure(self):
        result = self.append("a fact", push=True, verifier=NO_REMOTE)
        self.assertEqual(result.status, COMMITTED)
        self.assertEqual(result.push.status, PUSH_LOCAL_ONLY)
        self.assertEqual(result.push.backlog.count, 1)

    def test_backlog_survives_a_corrupt_file_as_empty(self):
        path = default_backlog_path(self.hub)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertEqual(read_backlog(path), Backlog())

    def test_the_same_commit_is_not_recorded_twice(self):
        path = default_backlog_path(self.hub)
        record_unpushed(path, "abc123", NOW, "offline")
        backlog = record_unpushed(path, "abc123", NOW + timedelta(minutes=5), "still offline")
        self.assertEqual(backlog.count, 1)
        self.assertEqual(backlog.last_error, "still offline")


class TestDivergence(HubRepoCase):
    def setUp(self):
        super().setUp()
        self.remote = self.bare_remote()
        git("-C", str(self.hub), "remote", "add", "origin", str(self.remote))
        git("-C", str(self.hub), "push", "-q", "origin", "main")
        # somebody else pushed in the meantime — the remote now holds a commit we do not
        self.other = self.root / "other"
        git("clone", "-q", str(self.remote), str(self.other))
        (self.other / "notes.md").write_text(self.SEED + "- written elsewhere\n")
        git("-C", str(self.other), "commit", "-q", "-am", "elsewhere")
        git("-C", str(self.other), "push", "-q", "origin", "main")
        self.remote_head = git("-C", str(self.remote), "rev-parse", "refs/heads/main").strip()

        self.result = self.append("a fact recorded here", push=True, verifier=PRIVATE_REMOTE)

    def test_the_write_is_recorded_but_the_push_halts(self):
        self.assertEqual(self.result.status, COMMITTED)
        self.assertEqual(self.result.push.status, DIVERGED)
        self.assertIn("HALTED", self.result.push.detail)

    def test_the_remote_commit_is_not_discarded(self):
        self.assertEqual(
            git("-C", str(self.remote), "rev-parse", "refs/heads/main").strip(), self.remote_head
        )

    def test_nothing_was_merged_into_the_hub(self):
        subjects = git("-C", str(self.hub), "log", "--pretty=%s")
        self.assertNotIn("written elsewhere", subjects)
        self.assertNotIn("Merge", subjects)
        self.assertNotIn("- written elsewhere", self.notes.read_text())

    def test_it_is_recorded_in_the_backlog_for_review(self):
        self.assertEqual(self.result.push.backlog.count, 1)


# --- the verb -------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    """Only the pre-mutation refusal runs here: main() targets the REAL hub location."""

    def test_a_refused_hub_path_exits_1_before_touching_anything(self):
        self.addCleanup(restore_env, "BUZAI_HUBS_DIR", os.environ.get("BUZAI_HUBS_DIR"))
        os.environ["BUZAI_HUBS_DIR"] = str(REPO_ROOT)  # inside the public checkout
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = main(["--file", "notes.md", "--append", "- x", "--summary", "s"])
        self.assertEqual(rc, 1)
        self.assertIn("hub-commit FAIL", err.getvalue())

    def test_retry_push_on_a_local_only_hub_exits_0(self):
        """A hub with no remote must not fail a service-start retry: that is a
        durability warning, not a startup blocker. Safe to run against the real
        verifier — `hub_remote.verify` returns local-only without probing anything."""
        case = HubRepoCase("run")
        case.setUp()
        self.addCleanup(case.tearDown)
        self.addCleanup(restore_env, "BUZAI_HUBS_DIR", os.environ.get("BUZAI_HUBS_DIR"))
        os.environ["BUZAI_HUBS_DIR"] = str(case.hub)
        record_unpushed(default_backlog_path(case.hub), "deadbeef", NOW, "offline earlier")

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(["--retry-push"])

        self.assertEqual(rc, 0)
        self.assertIn("no remote is configured", err.getvalue())

    def test_an_uninitialized_hub_is_reported_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.addCleanup(restore_env, "BUZAI_HUBS_DIR", os.environ.get("BUZAI_HUBS_DIR"))
            os.environ["BUZAI_HUBS_DIR"] = str(Path(tmp) / "hubs")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = main(["--file", "notes.md", "--append", "- x", "--summary", "s"])
            self.assertEqual(rc, 1)
            self.assertIn("make hub-init", err.getvalue())
            self.assertFalse((Path(tmp) / "hubs").exists())


if __name__ == "__main__":
    unittest.main()
