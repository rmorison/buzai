"""U4: the gate — orchestrates resolve → classify → accumulate → enforce → audit.

Order of checks (HTD):
  1. Resolve tier from provenance (metadata only).
  2. Classify this call's legs; accumulate across the session (sticky untrusted).
  3. Protected-config edit? allow only owner-tier / no-untrusted-content task (R8).
  4. Always-gate list (incl. new-recipient sends)? → ASK.
  5. All three legs accumulated? → DENY with a decompose-and-reapprove reason (R5).
  6. Otherwise policy tier: review→ASK, notify→ALLOW(+notify), auto→ALLOW.
Every path writes one redacted audit entry. Errors are the caller's (hook.py)
job to map to DENY — see KTD5.
"""

from __future__ import annotations

import re

from . import audit, classify, legstate, policy, provenance, tiers
from .always_gate import is_always_gated
from .config_loader import load_config
from .model import ALL_LEGS, Decision, GateResult, Leg, Tier

# basenames of the configs that must not be edited by an untrusted task
PROTECTED_CONFIG_NAMES = {
    "policy.toml",
    "policy.local.toml",
    "always-gate.toml",
    "always-gate.local.toml",
    "known-recipients.toml",
    "known-recipients.local.toml",
    "tiers.toml",
    "tiers.local.toml",
    "connector-legs.toml",
    "connector-legs.local.toml",
}
_WRITE_TOOL = re.compile(r"(?i)(write|edit|update|delete|remove|create|put|patch|move)")


class GateConfig:
    """Bundles loaded configs + state/audit dirs so tests can inject fixtures."""

    def __init__(self, config_dir=None, state_dir=None, audit_dir=None, workspace=None):
        self.tiers = load_config("tiers", config_dir)
        self.legs = load_config("connector-legs", config_dir)
        self.policy = load_config("policy", config_dir)
        self.always_gate = load_config("always-gate", config_dir)
        self.known_recipients = load_config("known-recipients", config_dir)
        self.state_dir = state_dir or ".buzai/legstate"
        self.audit_dir = audit_dir or "audit"
        self.workspace = workspace


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for val in obj.values():
            yield from _iter_strings(val)
    elif isinstance(obj, (list, tuple)):
        for val in obj:
            yield from _iter_strings(val)


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("/\\"))[-1] or path


def _writes_protected_config(tool_name: str, tool_input: dict) -> bool:
    if not _WRITE_TOOL.search(tool_name or ""):
        return False
    # Walk the whole input (nested dicts / lists — e.g. MultiEdit's edits[].path),
    # and use a cross-platform basename so a backslash path can't slip past.
    for s in _iter_strings(tool_input or {}):
        if _basename(s) in PROTECTED_CONFIG_NAMES:
            return True
    return False


# The gate's own trust state — provenance (the turn's tier) and legstate (the
# accumulated trifecta legs) — lives under this workspace dir. It is written ONLY
# by the gate itself and the UserPromptSubmit provenance hook, never by a tool call.
# A tool write here could forge the owner tier or reset accumulated legs, so it is
# denied UNCONDITIONALLY (stricter than the leg-conditional protected-config rule:
# even a clean no-untrusted-content task may not touch it).
TRUST_STATE_DIR = ".buzai"


def _writes_trust_state(tool_name: str, tool_input: dict) -> bool:
    if not _WRITE_TOOL.search(tool_name or ""):
        return False
    for s in _iter_strings(tool_input or {}):
        if TRUST_STATE_DIR in re.split(r"[\\/]", s):  # a path component, not a substring
            return True
    return False


def gate(event: dict, cfg: GateConfig) -> GateResult:
    session_id = event.get("session_id", "")
    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {}) or {}

    prov = provenance.read_provenance(cfg.workspace)
    tier = tiers.resolve_tier(prov, cfg.tiers)
    this_legs = classify.classify(tool_name, tool_input, cfg.legs)
    # Scope trifecta leg accumulation to the TURN, not the whole session: the
    # per-turn provenance writer stamps an incrementing turn_id, so a new turn gets
    # a fresh leg set and a prior turn's legs can't brick the current one. A missing
    # turn_id falls back to session-lifetime scoping (conservative — over-accumulate,
    # never under-accumulate), preserving the fail-closed contract.
    turn_id = prov.get("turn_id")
    leg_scope = f"{session_id}:{turn_id}" if isinstance(turn_id, int) else session_id
    acc = legstate.accumulate(leg_scope, this_legs, cfg.state_dir)
    ac = policy.action_class(tool_name, cfg.policy)

    def finish(result: GateResult, *, trifecta_warning: bool = False) -> GateResult:
        entry = {
            "session": session_id,
            "tool": tool_name,
            "tool_input": tool_input,
            "tier": tier.name,
            "legs": sorted(leg.value for leg in acc),
            "action_class": ac,
            "decision": result.decision.value,
            "reason": result.reason,
        }
        # Higher-signal marker so log review can find every time the owner approved
        # through a completed trifecta (distinct from an ordinary policy:review ASK).
        # Kept legible by redact._VERBATIM_ENTRY_KEYS.
        if trifecta_warning:
            entry["trifecta_warning"] = True
        audit.record(entry, cfg.audit_dir)
        return result

    # 2.5 trust-state integrity — provenance/legstate are never tool-writable.
    # Unconditional (independent of tier/legs): a write here could forge the owner
    # tier or wipe accumulated legs, defeating the gate from inside.
    if _writes_trust_state(tool_name, tool_input):
        return finish(
            GateResult(
                Decision.DENY,
                "trust-state files under .buzai/ (provenance, legstate) are not writable "
                "by a tool call",
                tier=tier,
                legs=acc,
                action_class=ac,
            )
        )

    # 3. protected-config edit authority
    if _writes_protected_config(tool_name, tool_input):
        if not policy.can_edit_protected(tier, acc):
            return finish(
                GateResult(
                    Decision.DENY,
                    "edit to a protected trust config is allowed only from the owner "
                    "or a task that has not ingested untrusted content",
                    tier=tier,
                    legs=acc,
                    action_class=ac,
                )
            )

    # 4. always-gate list (pass the send-leg so unmapped send tools still gate)
    gated, reason = is_always_gated(
        ac,
        tool_name,
        tool_input,
        cfg.always_gate,
        cfg.known_recipients,
        has_external_send=Leg.EXTERNAL_SEND in this_legs,
    )
    if gated:
        return finish(GateResult(Decision.ASK, reason, tier=tier, legs=acc, action_class=ac))

    # 5. Rule of Two — evaluated against accumulated legs (this turn)
    if ALL_LEGS <= acc:
        if tier == Tier.OWNER:
            # The owner is present and individually gates every send — the bottom
            # bread of the "AI sandwich". A fully-automated refusal would remove the
            # human at exactly the moment judgment matters, so escalate attention with
            # a loud, leg-naming ASK instead of a hard DENY. (Non-owner tiers below
            # have no human on the loop, so they keep the deny.)
            return finish(
                GateResult(
                    Decision.ASK,
                    "this turn now holds all three trifecta legs (private read + untrusted "
                    "content + external send). Untrusted content you've read may be steering "
                    "this action — approve only if you recognize and intend it.",
                    tier=tier,
                    legs=acc,
                    action_class=ac,
                ),
                trifecta_warning=True,
            )
        return finish(
            GateResult(
                Decision.DENY,
                "this task has accumulated all three trifecta legs (private read + "
                "untrusted content + external send); decompose into a no-untrusted-input "
                "privileged step and re-request after approval",
                tier=tier,
                legs=acc,
                action_class=ac,
            )
        )

    # 6. standing policy
    ptier = policy.tier_for(ac, cfg.policy)
    # An unmapped tool (no connector-legs rule) must not run silently even if its
    # action class was promoted to auto/notify: the gate can't reason about an
    # unclassified tool's blast radius, so a promotion could let a send through
    # ungated. Cap it at ask. Intentional zero-leg tools (ToolSearch, reads, local
    # writes) ARE mapped, so they keep their promotions. This closes the
    # unmapped-tool + auto-policy footgun that the softened unknown-tool default
    # would otherwise open — map the tool (even to zero legs) to promote it.
    if ptier in (policy.AUTO, policy.NOTIFY) and not classify.is_mapped(tool_name, cfg.legs):
        return finish(
            GateResult(
                Decision.ASK,
                f"unmapped tool '{tool_name}' is set to policy:{ptier} but has no "
                "connector-legs classification — gating (ask) until it is classified",
                tier=tier,
                legs=acc,
                action_class=ac,
            )
        )
    if ptier == policy.AUTO:
        return finish(
            GateResult(
                Decision.ALLOW, f"policy:auto for '{ac}'", tier=tier, legs=acc, action_class=ac
            )
        )
    if ptier == policy.NOTIFY:
        return finish(
            GateResult(
                Decision.ALLOW,
                f"policy:notify for '{ac}' — will report with undo",
                notify=True,
                tier=tier,
                legs=acc,
                action_class=ac,
            )
        )
    return finish(
        GateResult(
            Decision.ASK,
            f"policy:review for '{ac}' — approval required",
            tier=tier,
            legs=acc,
            action_class=ac,
        )
    )
