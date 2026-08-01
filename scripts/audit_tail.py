"""Show the last few trust-gate decisions from the audit log, readably.

`make audit` — the one-liner that hides `audit/audit.jsonl`'s hash-chained JSON. Used to
*verify the gate is firing* (every tool call lands here with its tier and decision) and to
spot-check what the assistant has been allowed/asked/denied. For the full per-connector +
subagent verification, see `trust/tests/smoke_substrate.md`.

`format_entries` is pure (testable); `main` reads the real log and handles the
not-yet-written case.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LOG = Path(__file__).resolve().parents[1] / "audit" / "audit.jsonl"


def format_entries(lines, n: int = 15) -> list[str]:
    """Compact one-line-per-decision view of the last `n` audit lines. Each stored line is
    `{"prev":…, "entry":{ts,tool,tier,decision,legs,…}, "hash":…}`; tolerate a bare entry
    too, and skip anything unparseable rather than crashing."""
    rows = []
    for line in [ln for ln in lines if ln.strip()][-n:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        e = e.get("entry", e)
        legs = ",".join(e.get("legs", [])) or "-"
        rows.append(
            f"{e.get('ts','?'):<25} {e.get('decision','?'):<6} "
            f"{e.get('tier','?'):<6} {e.get('tool','?')}  [{legs}]"
        )
    return rows


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    n = 15
    if "-n" in argv:
        try:
            n = int(argv[argv.index("-n") + 1])
        except (ValueError, IndexError):
            print("usage: audit_tail.py [-n N]", file=sys.stderr)
            return 2
    if not LOG.exists() or LOG.stat().st_size == 0:
        print(
            "no gate decisions logged yet — ask the assistant to do one small thing "
            "(every tool call is gated and logged), then re-run `make audit`."
        )
        return 0
    rows = format_entries(LOG.read_text().splitlines(), n)
    print("\n".join(rows) if rows else "audit log present but no readable entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
