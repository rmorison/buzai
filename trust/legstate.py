"""U9: session-scoped accumulation of trifecta legs.

The trifecta accumulates across separate tool calls in a task, so Rule-of-Two is
evaluated against the union of legs seen so far this session, not the single call.
State is keyed on the hook's session_id. The untrusted-content leg is sticky:
once a session ingests untrusted content, every later call in that session is
treated as carrying it (union never removes — KTD6/KTD8).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .model import Leg

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _state_path(session_id: str, state_dir) -> Path:
    safe = _SAFE.sub("_", session_id or "no-session")
    return Path(state_dir) / f"{safe}.json"


def _load(path: Path) -> set:
    try:
        with path.open("rb") as fh:
            return set(json.load(fh))
    except (FileNotFoundError, ValueError, OSError):
        return set()


def accumulate(session_id: str, this_legs, state_dir) -> set[Leg]:
    """Union this call's legs into the session's accumulated set and persist it."""
    path = _state_path(session_id, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    acc = _load(path)
    acc |= {leg.value for leg in this_legs}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(acc)))
    tmp.replace(path)
    return {Leg(x) for x in acc}
