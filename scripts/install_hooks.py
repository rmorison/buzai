"""Wire the trust-gate hooks into the workspace .claude/settings.json.

Replaces the brittle "hand the hook-merge to your claude session as a prompt" setup
step (SETUP §5b) with a deterministic, idempotent, non-destructive merge: the buzai
PreToolUse (gate) and UserPromptSubmit (mark-turn) hooks from trust/settings.example.json
are merged into your existing settings.json, preserving every unrelated key and never
duplicating a hook on re-run. A malformed existing settings.json HARD-FAILS rather than
being overwritten — your file is never clobbered.

Core merge logic (`merge_hooks`) and the file step (`install`) are kept injectable for
testing; `main` wires the real repo paths.
"""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "trust" / "settings.example.json"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"


def _norm(cmd):
    """Normalize a command for dedup: drop quotes and surrounding whitespace, so
    `"$CLAUDE_PROJECT_DIR"/trust/bin/gate` and `$CLAUDE_PROJECT_DIR/trust/bin/gate`
    are recognized as the same hook and the gate isn't double-wired."""
    return cmd.replace('"', "").replace("'", "").strip() if isinstance(cmd, str) else cmd


def _commands(group: dict) -> set:
    """The normalized command strings of a hook group — its identity for dedup.
    Command-less hooks (no `command` key) contribute nothing, so they can't collapse
    to a spurious shared identity."""
    return {
        _norm(h.get("command"))
        for h in group.get("hooks", [])
        if h.get("type") == "command" and h.get("command")
    }


def merge_hooks(existing: dict, example: dict) -> dict:
    """Return `existing` with `example`'s hook groups merged in (pure, non-destructive).

    Only the `hooks` key is touched; all other keys are preserved verbatim. A group is
    appended only if none of its commands already appear under that event, so re-running
    is a no-op (idempotent) and a pre-existing non-buzai hook is kept alongside, not
    clobbered.

    Raises ValueError (which `main` reports as a clean failure) if `existing` isn't a
    JSON object, or its `hooks` / a hooks event isn't the expected object/array shape —
    a hand-mangled settings.json fails loudly instead of crashing with a traceback.
    """
    if not isinstance(existing, dict):
        raise ValueError(f"settings must be a JSON object, got {type(existing).__name__}")
    result = deepcopy(existing)
    dest_hooks = result.setdefault("hooks", {})
    if not isinstance(dest_hooks, dict):
        raise ValueError("'hooks' must be a JSON object")
    for event, groups in example.get("hooks", {}).items():
        dest = dest_hooks.setdefault(event, [])
        if not isinstance(dest, list):
            raise ValueError(f"hooks.{event} must be a JSON array")
        present = set().union(*(_commands(g) for g in dest)) if dest else set()
        for g in groups:
            if _commands(g) & present:
                continue  # already wired -> idempotent
            dest.append(deepcopy(g))
            present |= _commands(g)
    return result


def install(target: Path, example: Path) -> str:
    """Merge `example`'s hooks into `target`, writing only on change. Returns one of
    "noop" | "created" | "merged". Raises ValueError on malformed target JSON WITHOUT
    writing, so a corrupt settings.json is never overwritten."""
    example_data = json.loads(example.read_text())
    existing: dict = {}
    if target.exists():
        text = target.read_text()
        if text.strip():
            try:
                existing = json.loads(text)
            except json.JSONDecodeError as e:
                raise ValueError(f"{target} is not valid JSON ({e}); refusing to overwrite") from e
    merged = merge_hooks(existing, example_data)
    if merged == existing:
        return "noop"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(merged, indent=2) + "\n")
    return "created" if not existing else "merged"


def main(argv=None) -> int:
    try:
        status = install(SETTINGS, EXAMPLE)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"install-hooks FAIL: {e}", file=sys.stderr)
        return 1
    print(
        {
            "noop": "install-hooks: trust-gate hooks already present — no change",
            "created": f"install-hooks: created {SETTINGS} with the trust-gate hooks",
            "merged": f"install-hooks: merged the trust-gate hooks into {SETTINGS}",
        }[status]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
