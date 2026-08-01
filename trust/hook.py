"""U4 (shim): the PreToolUse hook entry point.

Reads the event JSON on stdin, runs the gate under a short internal timeout, and
prints the permissionDecision. Any timeout, exception, or unparseable input maps
to DENY — fail-closed is engineered here, NOT inherited from the harness, whose
default on hook timeout is to proceed (KTD5).

Register in .claude/settings.json:
    {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "python3 -m trust.hook"}]}]}}
"""

from __future__ import annotations

import json
import sys
import threading

from .gate import GateConfig, gate
from .model import Decision, GateResult

INTERNAL_TIMEOUT_S = 5.0


def _deny(reason: str) -> GateResult:
    return GateResult(Decision.DENY, reason)


def evaluate(event: dict, cfg: GateConfig = None) -> GateResult:
    cfg = cfg or GateConfig()
    box = {}

    def run():
        try:
            box["result"] = gate(event, cfg)
        except Exception as exc:  # fail closed on any gate error
            box["result"] = _deny(f"gate error — failing closed: {exc!r}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(INTERNAL_TIMEOUT_S)
    if t.is_alive():
        return _deny("gate timed out — failing closed")
    return box.get("result") or _deny("gate produced no decision — failing closed")


def main(argv=None) -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw.strip() else {}
        if not isinstance(event, dict):
            event = {}
    except ValueError:
        result = _deny("unparseable hook input — failing closed")
        print(json.dumps(result.to_permission_output()))
        return 0
    result = evaluate(event)
    print(json.dumps(result.to_permission_output()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
