"""Liveness probe: treat the always-on assistant as live iff its `claude remote-control`
process holds an established `:443` socket (an ESTAB connection to port 443 owned by that
process's PID). In practice the registered server holds a persistent relay control
connection, so this is a good proxy — but the check proves "the process holds SOME HTTPS
connection," not strictly "it is registered with the relay": `claude` also opens :443 to
the API during turns. The narrow false-LIVE window (registration dropped AND an unrelated
:443 happens to be open at probe time) is acceptable for MVP; relay-vs-API disambiguation
(peer-IP allowlist, or longest-lived socket) is deferred to the self-healing track. The
journald "Ready" banner is NOT a liveness signal (often never emitted).

Why PID-scoped, not peer-host-scoped: `ss -tnp` emits numeric peer IPs (never the
relay hostname), so matching a `claude.ai` peer never fired — the original bug. Scoping
to the remote-control process's own PID is correct and not spoofable by another process's
:443 (the users-column PID is kernel-attributed); the residual is the API-vs-relay
ambiguity noted above, not a spoof.

Turn-freshness (`last_good_turn_age`) is INFORMATIONAL only, never a NOT-LIVE cause: an
always-on assistant is legitimately idle for long stretches yet still live/attachable.
Detecting a wedged-but-connected session is the deferred self-healing track, not MVP
liveness. Core logic (`socket_established`, `last_good_turn_age`, `is_live`) is pure.
"""

from __future__ import annotations

import re
import time
from datetime import datetime

RELAY_PORT = "443"  # the relay control connection rides https/443
TURN_MARKER = "turn completed"  # informational only; set to the real marker if one exists
MAX_TURN_AGE_S = 900  # informational staleness threshold (15 min)
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


def socket_established(ss_text: str, pid, relay_port: str = RELAY_PORT) -> bool:
    """True iff an `ss -tnp` line shows an ESTAB connection to `relay_port` owned by
    process `pid`. The peer column is numeric (IP:port), so we match the peer *port*
    and the owning *PID* (in the `users:((...,pid=N,...))` column) — not a hostname.
    `pid` may be int or str; a falsy pid yields False."""
    if not pid:
        return False
    needle = f"pid={pid},"
    for line in (ss_text or "").splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] != "ESTAB":
            continue
        if fields[4].rsplit(":", 1)[-1] != relay_port:  # peer port column
            continue
        if needle in line:  # owned by the remote-control PID
            return True
    return False


def last_good_turn_age(journal_text: str, marker: str = TURN_MARKER, now: float | None = None):
    """Seconds since the most recent line containing `marker` with a timestamp, or None
    if none is found. Informational only (see module docstring)."""
    now = time.time() if now is None else now
    latest = None
    for line in (journal_text or "").splitlines():
        if marker not in line:
            continue
        m = _TS_RE.search(line)
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(f"{m.group(1)} {m.group(2)}").timestamp()
        except ValueError:
            continue
        latest = ts if latest is None else max(latest, ts)
    return None if latest is None else now - latest


def is_live(
    ss_text, pid, journal_text=None, *, marker=TURN_MARKER, max_age_s=MAX_TURN_AGE_S, now=None
):
    """Return (live: bool, reason: str). Live iff the remote-control PID holds an
    established relay socket. Turn-freshness is appended as context only — a stale or
    absent turn never makes an otherwise-connected always-on assistant NOT-LIVE."""
    if not socket_established(ss_text, pid):
        return False, "no established relay socket for the remote-control process"
    if journal_text is not None:
        age = last_good_turn_age(journal_text, marker, now)
        if age is not None:
            note = "" if age <= max_age_s else f" (stale: >{max_age_s}s — idle or wedged)"
            return True, f"relay socket established; last activity {int(age)}s ago{note}"
    return True, "relay socket established (idle — no recent turn in logs)"


def main(argv=None) -> int:
    import subprocess

    def _run(cmd) -> str:
        # A liveness probe must never hang on a wedged journald/ss/systemctl — bound it
        # and treat a timeout/missing tool as "no signal" (→ NOT-LIVE), never block.
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""

    pid = _run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "claude-remote"]).strip()
    if not pid or pid == "0":
        print("NOT-LIVE: claude-remote service has no running main process")
        return 1
    ss = _run(["ss", "-tnp"])
    jr = _run(["journalctl", "--user", "-u", "claude-remote", "-o", "cat", "--no-pager"])
    live, reason = is_live(ss, pid, jr)
    print(("LIVE: " if live else "NOT-LIVE: ") + reason)
    return 0 if live else 1


if __name__ == "__main__":
    raise SystemExit(main())
