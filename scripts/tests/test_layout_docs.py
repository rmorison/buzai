"""The deployment lives at ~/buzai-assistant, and no living doc may still send an owner to
~/buzai.

Issue #17. The Remote Control picker labels an environment by its working directory's
basename, so a deployment at ~/buzai and a dev checkout of the repo both show as
`buzai · <host>`. The installer, the unit and `make service-install` moved; a doc that
still says `cd ~/buzai` would put an owner back in the old layout, or in a directory
that does not exist.

The scan covers the files an owner follows today. Dated records (`docs/plans`,
`docs/decisions`, `docs/brainstorms`, `docs/ideation`, `docs/solutions`) describe what
was true when written and are left alone. Two kinds of line must name the old path and
are allowed to: a line that also names `buzai-assistant` (it is about the move), and the
SETUP.md migration section, which walks an owner out of ~/buzai.

Verified against the unfixed tree: the guard failed, listing the stale lines in
SETUP.md, QUICKSTART.md, HOST-BOOTSTRAP.md, TROUBLESHOOTING.md, TRY-IT.md and
deploy/README.md.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "docs" / "SETUP.md"
MIGRATION = "Moving an existing install to ~/buzai-assistant"

# The old workspace, however a doc spells its root: `~`, `$HOME`, systemd's `%h`, or
# the service account's home. Not `~/buzai-assistant`, not `~/.config/buzai`, not
# `~/.ssh/buzai-hub`, and not the account name in `ssh buzai@host` or `/home/buzai/.ssh`.
OLD_WORKSPACE = re.compile(r"(?:~|\$HOME|\$\{HOME\}|%h|/home/buzai)/buzai(?![\w-])")


def living_docs() -> list[Path]:
    top = [
        REPO_ROOT / name
        for name in ("README.md", "CONCEPTS.md", "CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md")
    ]
    files = (
        top
        + sorted((REPO_ROOT / "docs").glob("*.md"))
        + sorted((REPO_ROOT / "docs" / "dev").glob("*.md"))
    )
    files += [
        REPO_ROOT / "deploy" / "README.md",
        REPO_ROOT / "deploy" / "claude-remote.service.template",
        REPO_ROOT / "install.sh",
        REPO_ROOT / "Makefile",
    ]
    return [path for path in files if path.exists()]


def migration_section(text: str) -> tuple[int, int]:
    """Line span (start, end) of the SETUP.md migration section, heading included."""
    lines = text.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("#") and MIGRATION in line), None
    )
    if start is None:
        return (0, 0)
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith("#") and len(lines[i]) - len(lines[i].lstrip("#")) <= level
        ),
        len(lines),
    )
    return (start, end)


def stale_lines(path: Path, text: str) -> list[str]:
    span = migration_section(text) if path == SETUP else (0, 0)
    found = []
    for number, line in enumerate(text.splitlines()):
        if span[0] <= number < span[1] or "buzai-assistant" in line:
            continue
        if OLD_WORKSPACE.search(line):
            found.append(f"{path.relative_to(REPO_ROOT)}:{number + 1}: {line.strip()}")
    return found


class TestNoLivingDocSendsTheOwnerToTheOldWorkspace(unittest.TestCase):
    def test_no_living_doc_names_the_old_workspace(self):
        stale = [hit for path in living_docs() for hit in stale_lines(path, path.read_text())]
        self.assertEqual(stale, [], "\n" + "\n".join(stale))

    def test_the_scan_reaches_the_docs_that_carried_it(self):
        # a guard that silently scans nothing passes forever
        names = {path.relative_to(REPO_ROOT).as_posix() for path in living_docs()}
        for name in ("docs/SETUP.md", "docs/QUICKSTART.md", "deploy/README.md", "install.sh"):
            self.assertIn(name, names)


class TestTheGuardMatchesWhatItMeans(unittest.TestCase):
    """The pattern's edges, on planted lines. The account is also called `buzai`, so a
    loose pattern would flag every `ssh buzai@host`; a tight one would miss `%h/buzai`."""

    def hits(self, line: str) -> bool:
        return bool(stale_lines(REPO_ROOT / "planted.md", line))

    def test_it_catches_the_old_workspace_however_spelled(self):
        for line in (
            "cd ~/buzai",
            "git clone https://example.invalid/buzai.git ~/buzai && cd ~/buzai",
            "tail -n 5 ~/buzai/audit/audit.jsonl",
            "git -C $HOME/buzai pull",
            "sudo -u buzai git -C /home/buzai/buzai pull",
            "WorkingDirectory=%h/buzai",
            "The first `claude` run inside `~/buzai`.",
        ):
            self.assertTrue(self.hits(line), line)

    def test_it_leaves_the_rest_of_buzai_alone(self):
        for line in (
            "cd ~/buzai-assistant",
            "mkdir -p ~/.config/buzai/secrets",
            "ssh buzai@<host>",
            "sudo tee -a /home/buzai/.ssh/authorized_keys",
            "IdentityFile ~/.ssh/buzai-hub",
            "mv ~/buzai ~/buzai-assistant",  # names the new path: it is about the move
        ):
            self.assertFalse(self.hits(line), line)

    def test_the_migration_section_may_name_the_old_path(self):
        text = f"# Setup\n\n## {MIGRATION}\n\ncd ~/buzai\n\n## Next\n\ncd ~/buzai\n"
        stale = stale_lines(SETUP, text)
        self.assertEqual(len(stale), 1, stale)  # only the line after the section
        self.assertIn(":9:", stale[0])


class TestTheMigrationIsDocumented(unittest.TestCase):
    def section(self) -> str:
        text = SETUP.read_text()
        start, end = migration_section(text)
        self.assertLess(start, end, f"SETUP.md has no '{MIGRATION}' section")
        return "\n".join(text.splitlines()[start:end])

    def test_it_rebuilds_the_venv(self):
        self.assertIn("make venv", self.section())

    def test_it_reinstalls_the_unit(self):
        self.assertIn("make service-install", self.section())

    def test_it_re_trusts_the_folder(self):
        # `claude remote-control` refuses an untrusted workspace rather than asking
        self.assertIn("trust this folder", self.section())

    def test_it_ends_on_the_liveness_check(self):
        self.assertIn("make liveness", self.section())


if __name__ == "__main__":
    unittest.main()
