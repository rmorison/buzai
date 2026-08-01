"""RB1/RB3: read and write the per-turn provenance signal.

Claude Code's PreToolUse stdin does NOT carry channel/trigger provenance, so a
per-turn writer (the UserPromptSubmit hook — see trust/mark_turn.py) writes a
small provenance file that this module reads at each tool call. The file path
comes from $BUZAI_PROVENANCE_FILE, else a default under the workspace. A
missing/unreadable file yields {} → the caller fails closed to the
untrusted-external tier (KTD5). This module reads transport metadata only; it
never reads message content (KTD6).

Provenance shape (all optional; absence is safe):
    channel = "<stable channel id>"   # mapped to a tier by tiers.toml
    kind    = "owner|automation|known|unknown"   # fallback when channel unmapped
    turn_id = <int>                   # per-turn counter stamped each UserPromptSubmit
                                      # (mark_turn.py). The gate scopes trifecta
                                      # leg-state to session_id+turn_id, so legs reset
                                      # each turn. Absent → the gate falls back to
                                      # session-lifetime scoping (conservative).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ENV_VAR = "BUZAI_PROVENANCE_FILE"
DEFAULT_REL = ".buzai/provenance.json"


def provenance_path(workspace=None) -> Path:
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env)
    root = Path(workspace) if workspace else Path.cwd()
    return root / DEFAULT_REL


def read_provenance(workspace=None) -> dict:
    """Return the injected provenance dict, or {} when absent/unreadable.

    {} is the fail-closed signal — callers treat it as untrusted-external.
    """
    path = provenance_path(workspace)
    try:
        with path.open("rb") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def write_provenance(prov: dict, workspace=None) -> Path:
    """Write the per-turn provenance dict atomically; return the path written.

    Symmetric with read_provenance (same path resolution). Writes to a temp file
    and atomically replaces, so a concurrent reader never observes a half-written
    file mid-turn; the parent dir is created if absent. On a write error the
    exception propagates and any existing file is left untouched — absence and a
    stale file both resolve fail-closed downstream, so the gate is never weakened
    by a writer hiccup.
    """
    path = provenance_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(prov))
    tmp.replace(path)
    return path
