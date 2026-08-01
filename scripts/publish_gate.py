"""Leak gate for the public repo — successor to sync-eval.sh's FORBID check.

Scans text files for forbidden patterns and fails loudly, naming path:line. Runs as
a pre-commit hook (the primary gate — it fires BEFORE anything is world-readable)
and in CI (the backstop). See docs/dev/PUBLIC-SEED.md for the policy.

The pattern data is deliberately split:

  * GENERIC_PATTERNS below ship in-repo — structural markers that are safe to show
    (they don't enumerate anything sensitive).
  * The literal PII/personal-infra patterns (real hostnames, account handles, box
    identifiers) live in a PRIVATE file OUTSIDE any repo — default
    ~/.config/buzai/gate-patterns, override with $BUZAI_GATE_PATTERNS — one regex
    per line, '#' comments allowed. On the maintainer's machine the gate enforces
    both sets; elsewhere (CI, contributors) it runs the generic set and notes the
    reduced coverage. Publishing the literal list would defeat the gate, so it is
    never committed (the gate itself hard-fails on a tracked file at that path
    shape via the policy in PUBLIC-SEED.md).

Files that legitimately *discuss* the gate are exempt (this file, its tests, the
policy doc) — a leak checker that trips on its own documentation trains people to
skip it.

Usage:
    publish_gate.py [FILE ...]     # check the given files (pre-commit passes these)
    publish_gate.py                # no args: check every git-tracked file
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Structural markers, safe to ship: private working-notes artifacts that must never
# be referenced from public content. Literal PII/infra patterns do NOT belong here —
# they go in the private pattern file.
GENERIC_PATTERNS = [
    r"SESSION-HANDOFF\.md",
]

# Repo-relative paths allowed to contain the patterns they document/enforce.
EXEMPT = {
    "scripts/publish_gate.py",
    "scripts/tests/test_publish_gate.py",
    "docs/dev/PUBLIC-SEED.md",
}

PRIVATE_PATTERNS_DEFAULT = "~/.config/buzai/gate-patterns"


def load_private_patterns(path: str | None = None) -> tuple[list[str], bool]:
    """(patterns, file_present). Missing file is normal off the maintainer machine."""
    p = Path(path or os.environ.get("BUZAI_GATE_PATTERNS", PRIVATE_PATTERNS_DEFAULT)).expanduser()
    if not p.is_file():
        return [], False
    pats = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append(line)
    return pats, True


def scan_files(paths: list[Path], patterns: list[str], root: Path = REPO_ROOT) -> list[str]:
    """['relpath:lineno: matched <pattern>'] for every hit in non-exempt text files. Pure
    w.r.t. its inputs; binary and unreadable files are skipped."""
    compiled = [re.compile(p) for p in patterns]
    findings = []
    for path in paths:
        try:
            rel = str(path.resolve().relative_to(root))
        except ValueError:
            rel = str(path)
        if rel in EXEMPT:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable — gitleaks covers binaries' secret shapes
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat in compiled:
                if pat.search(line):
                    findings.append(f"{rel}:{lineno}: matched {pat.pattern!r}")
    return findings


def tracked_files(root: Path = REPO_ROOT) -> list[Path]:
    r = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        print("publish-gate: git ls-files failed — not a git checkout?", file=sys.stderr)
        raise SystemExit(2)
    return [root / f for f in r.stdout.split("\0") if f]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    private, present = load_private_patterns()
    patterns = GENERIC_PATTERNS + private
    paths = [Path(a) for a in argv] if argv else tracked_files()
    findings = scan_files(paths, patterns)
    if not present:
        print(
            "publish-gate: note — private pattern file absent; running with the "
            "generic set only (full coverage lives on the maintainer's machine)"
        )
    if findings:
        print("publish-gate FAIL — forbidden pattern(s) found:", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print("see docs/dev/PUBLIC-SEED.md for the policy", file=sys.stderr)
        return 1
    print(f"publish-gate: clean ({len(paths)} files, {len(patterns)} patterns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
