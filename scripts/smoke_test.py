"""Drive and record the buzai MVP smoke test (SC0-SC8).

SC0 is a preflight GATE: if secrets/credentials are miswired, the rest is blocked
(don't start the service on an unsafe layout). The mechanizable checks (SC0 secrets,
SC1 liveness) run automatically; the live steps (app attach, connector actions,
restart, gate enforcement) are operator-attested checklist items recorded here.

Core (`evaluate`, `overall`) is pure for testing; `main` wires the live checks.
"""

from __future__ import annotations

import sys

CRITERIA = {
    "SC0": "Preflight: secrets dir + credentials.json 0600 and untracked (gate)",
    "SC1": "Service up + survives reboot — liveness by relay socket + last-good-turn",
    "SC2": "Remote Control attaches from desktop + mobile; reconnects after restart",
    "SC3": "A managed-connector action works through the gate",
    "SC4": "A local-MCP action works through the gate",
    "SC5": "Gate enforces: permitted action, review/new-recipient ask, all-three deny, 2nd-action still gates",
    "SC6": "Hubs readable/writable and persist across a restart",
    "SC7": "Substrate smoke-test recorded (incl. subagent coverage); audit/ is 0700",
    "SC8": "Connector auth survives a restart within token validity (no re-auth)",
}
_GATED = [k for k in CRITERIA if k != "SC0"]


def evaluate(results: dict) -> dict:
    """Map each criterion to pass | fail | pending | blocked.

    results: {sc_id: True|False|None}. SC0 False blocks SC1-SC8 (unsafe to proceed).
    """
    out = {}
    sc0 = results.get("SC0")
    out["SC0"] = "pass" if sc0 is True else ("fail" if sc0 is False else "pending")
    blocked = sc0 is False
    for sc in _GATED:
        v = results.get(sc)
        if blocked:
            out[sc] = "blocked"
        else:
            out[sc] = "pass" if v is True else ("fail" if v is False else "pending")
    return out


def overall(statuses: dict) -> str:
    return "PASS" if all(v == "pass" for v in statuses.values()) else "INCOMPLETE"


def render(statuses: dict) -> str:
    lines = [f"  {sc}  {statuses[sc]:<8} {CRITERIA[sc]}" for sc in CRITERIA]
    return "\n".join(lines) + f"\n  ----\n  OVERALL: {overall(statuses)}"


def main(argv=None) -> int:
    # The live checks (SC1-SC8 beyond preflight) require a running instance and the
    # apps; this entrypoint runs the mechanizable preflight and prints the checklist.
    from scripts import secrets_preflight

    results = {"SC0": secrets_preflight.main([]) == 0}
    statuses = evaluate(results)
    print(render(statuses))
    if statuses["SC0"] != "pass":
        print("\nSC0 preflight failed — fix secrets/credentials before starting.", file=sys.stderr)
        return 1
    print("\nSC0 passed. Complete SC1-SC8 against the running instance per docs/SMOKE-TEST.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
