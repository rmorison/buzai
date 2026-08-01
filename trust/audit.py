"""U7 (audit half): append a redacted, hash-chained audit entry per decision.

Each entry is redacted (redact.py), then chained to the prior entry's hash so
tampering is detectable on read (R13). The log lives in a dedicated audit dir the
gate writes; ordinary tool calls should not have write access to it (enforced at
deploy time by filesystem permissions — see trust/README.md).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .redact import redact_entry

GENESIS = "0" * 16


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _entry_hash(prev: str, payload: dict) -> str:
    return hashlib.sha256((prev + _canonical(payload)).encode("utf-8")).hexdigest()[:16]


def _log_path(audit_dir) -> Path:
    return Path(audit_dir) / "audit.jsonl"


def _last_hash(path: Path) -> str:
    """Return the last entry's hash by reading the tail of the file (O(1) in log
    length), not by scanning every line on every append."""
    if not path.exists():
        return GENESIS
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            buf = b""
            while end > 0 and buf.count(b"\n") < 2:
                step = min(4096, end)
                end -= step
                fh.seek(end)
                buf = fh.read(step) + buf
        for line in reversed(buf.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line).get("hash", GENESIS)
            except ValueError:
                return GENESIS
    except OSError:
        return GENESIS
    return GENESIS


def record(entry: dict, audit_dir, ts: str = None) -> dict:
    """Redact, chain, and append one entry. Returns the written record.

    A UTC ISO-8601 timestamp (`ts`) is stamped INSIDE the hashed entry so it is
    itself tamper-evident — altering it breaks the chain (the hash commits to it).
    The chain already fixes *ordering*; `ts` adds *wall-clock time* for correlating
    a decision against external events. Pass `ts` to make a test deterministic;
    otherwise the current time (second precision) is used.
    """
    path = _log_path(audit_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = _last_hash(path)
    stamped = {"ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
    payload = {"prev": prev, "entry": redact_entry(stamped)}
    payload["hash"] = _entry_hash(prev, payload["entry"])
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(payload) + "\n")
    return payload


def verify_chain(audit_dir) -> bool:
    """True if every entry's hash matches its (prev, entry) — i.e. untampered."""
    path = _log_path(audit_dir)
    if not path.exists():
        return True
    prev = GENESIS
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                return False  # a malformed line is tamper/corruption, not an error
            if rec.get("prev") != prev:
                return False
            if rec.get("hash") != _entry_hash(prev, rec.get("entry", {})):
                return False
            prev = rec["hash"]
    return True
