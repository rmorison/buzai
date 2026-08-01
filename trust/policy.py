"""U5: the standing trust policy — action class → auto/notify/review.

Unmatched action classes default to `review` (fail-closed). Edits to the
protected configs (policy, always-gate, known-recipients) are allowed only from
an owner-tier task or one carrying no untrusted-content leg (R8). The action
class for a tool is looked up from policy.toml's [classes-of] map, defaulting to
the tool name itself.
"""

from __future__ import annotations

from .model import Tier, Leg

REVIEW = "review"
NOTIFY = "notify"
AUTO = "auto"
_VALID = {REVIEW, NOTIFY, AUTO}


def action_class(tool_name: str, config: dict) -> str:
    mapping = config.get("classes_of", {}) or {}
    return mapping.get(tool_name, tool_name)


def tier_for(action_class_name: str, config: dict) -> str:
    classes = config.get("classes", {}) or {}
    val = classes.get(action_class_name, REVIEW)
    return val if val in _VALID else REVIEW


def can_edit_protected(provenance_tier: Tier, accumulated_legs) -> bool:
    """R8: owner-tier task, or any task that has not ingested untrusted content."""
    if provenance_tier == Tier.OWNER:
        return True
    return Leg.UNTRUSTED_CONTENT not in set(accumulated_legs)
