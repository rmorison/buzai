"""Shared types: trust tiers, trifecta legs, gate decisions."""

from __future__ import annotations

from enum import Enum, IntEnum


class Tier(IntEnum):
    """Trust tier of a turn, ordered low→high. IntEnum gives `>=` comparison."""

    UNTRUSTED_EXTERNAL = 0
    KNOWN_CONTACT = 1
    CONFIGURED_AUTOMATION = 2
    OWNER = 3


TIER_BY_NAME = {t.name.lower(): t for t in Tier}
# friendly aliases used in config
TIER_BY_NAME.update(
    {
        "owner": Tier.OWNER,
        "configured-automation": Tier.CONFIGURED_AUTOMATION,
        "configured_automation": Tier.CONFIGURED_AUTOMATION,
        "known-contact": Tier.KNOWN_CONTACT,
        "known_contact": Tier.KNOWN_CONTACT,
        "untrusted-external": Tier.UNTRUSTED_EXTERNAL,
        "untrusted_external": Tier.UNTRUSTED_EXTERNAL,
    }
)


class Leg(str, Enum):
    """The three legs of the 'lethal trifecta'."""

    PRIVATE_READ = "private_read"
    UNTRUSTED_CONTENT = "untrusted_content"
    EXTERNAL_SEND = "external_send"


ALL_LEGS = frozenset(Leg)


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class GateResult:
    """Result of a gate evaluation. `notify` flags an allow that the runtime
    should report after the fact with an undo affordance (policy tier 'notify')."""

    __slots__ = ("decision", "reason", "notify", "tier", "legs", "action_class")

    def __init__(self, decision, reason, *, notify=False, tier=None, legs=None, action_class=None):
        self.decision = decision
        self.reason = reason
        self.notify = notify
        self.tier = tier
        self.legs = set(legs or [])
        self.action_class = action_class

    def to_permission_output(self):
        """Shape Claude Code's PreToolUse hook expects."""
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": self.decision.value,
                "permissionDecisionReason": self.reason,
            }
        }
