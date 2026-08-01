"""U1: the per-turn provenance writer — the UserPromptSubmit hook entry point.

Claude Code fires UserPromptSubmit once per turn, before that turn's tool calls.
For this single-principal instance every prompt arrives over the owner's
authenticated Remote Control attach, so the turn is marked `owner` (KTD3). The
kind is overridable via $BUZAI_TURN_KIND, so a future automation/inbound injector
can reuse the same writer with its own provenance (e.g. BUZAI_TURN_KIND=automation).

A write failure exits non-zero but never deletes an existing file: absence and a
stale file both resolve fail-closed downstream (provenance → untrusted-external),
so the gate is never weakened by a writer hiccup.

Register in .claude/settings.json (see trust/settings.example.json):
    {"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": "\\"$CLAUDE_PROJECT_DIR\\"/trust/bin/mark-turn"}]}]}}
"""

from __future__ import annotations

import os
import sys

from .provenance import read_provenance, write_provenance

DEFAULT_KIND = "owner"


def main(argv=None) -> int:
    kind = os.environ.get("BUZAI_TURN_KIND") or DEFAULT_KIND
    # Stamp a per-turn counter so the gate can scope trifecta leg-state to the turn
    # (legs reset each UserPromptSubmit). Read the prior turn's id and increment;
    # a missing or non-int prior id starts the count at 1.
    prior = read_provenance().get("turn_id")
    turn_id = prior + 1 if isinstance(prior, int) else 1
    try:
        write_provenance({"kind": kind, "turn_id": turn_id})
    except OSError as exc:  # leave any existing file in place; fail-closed downstream
        print(f"buzai mark-turn: could not write provenance: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
