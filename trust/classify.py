"""U3: classify a single tool call into the trifecta legs it touches.

A connector→legs map (connector-legs.toml) assigns legs to tool-name patterns.
A rule may also set `ingests_untrusted = true` to mark calls that pull in
external/inbound content (an inbox read, a web fetch) — those raise the
untrusted-content leg by content provenance (KTD6), independent of the turn's
trigger tier. The stickiness of that leg across a task lives in legstate (U9).

An unknown/unmapped tool contributes NO legs (it does not poison the trifecta
accounting). It is not auto-run either: with no legs it falls through to the
standing policy, whose unmatched-class default is `review` → ASK, so the owner
approves an unrecognized tool once. This deliberately reverses the earlier
fail-conservative-to-all-three-legs default, which (combined with leg stickiness)
hard-denied and bricked the session on the first unmapped call — see the
gate-hardening notes in trust/README.md.
"""

from __future__ import annotations

from .model import Leg


def _matches(tool_name: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return tool_name.startswith(pattern[:-1])
    return tool_name == pattern


def _match_rule(tool_name: str, config: dict):
    best = None
    best_len = -1
    for rule in config.get("rule", []) or []:
        pat = rule.get("match", "")
        if _matches(tool_name, pat):
            # most specific (longest literal prefix) wins
            score = len(pat.rstrip("*"))
            if score > best_len:
                best, best_len = rule, score
    return best


def is_mapped(tool_name: str, config: dict) -> bool:
    """True if the tool matches a connector-legs rule (even one with empty legs).

    A tool matching NO rule is "unmapped". The gate refuses to honor an `auto`/`notify`
    policy promotion for an unmapped tool — it cannot reason about an unclassified
    tool's blast radius, so a promotion could otherwise silently allow a send. Note
    intentional zero-leg tools (ToolSearch, reads, local writes) ARE mapped, so this
    distinguishes "declared harmless" from "unknown".
    """
    return _match_rule(tool_name or "", config) is not None


def rule_legs(rule: dict) -> set[Leg]:
    """The legs a single connector-legs rule contributes: its declared `legs` plus
    UNTRUSTED_CONTENT when `ingests_untrusted` is set. The one place the rule→legs
    promotion lives, so `classify` and the trust-check coverage report can't drift."""
    legs = set()
    for name in rule.get("legs", []) or []:
        try:
            legs.add(Leg(name))
        except ValueError:
            continue
    if rule.get("ingests_untrusted"):
        legs.add(Leg.UNTRUSTED_CONTENT)
    return legs


def classify(tool_name: str, tool_input: dict, config: dict) -> set[Leg]:
    rule = _match_rule(tool_name or "", config)
    if rule is None:
        # Unknown tool — contribute NO legs (don't poison accumulation). The gate
        # still gates it: no legs → standing policy → unmatched-class default
        # `review` → ASK. Owner approves an unrecognized tool once; nothing
        # auto-runs and one unmapped call no longer bricks the session.
        return set()
    return rule_legs(rule)
