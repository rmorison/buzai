"""U2: resolve a turn's provenance to a trust tier — metadata only, fail-closed.

The resolver maps the provenance's `channel` (or, as a fallback, its `kind`) to a
tier using tiers.toml. Anything unmapped or absent resolves to UNTRUSTED_EXTERNAL.
It never inspects message content (KTD6) — only the provenance dict it is given.
"""

from __future__ import annotations

from .model import Tier, TIER_BY_NAME


def resolve_tier(provenance: dict, config: dict) -> Tier:
    if not provenance:
        return Tier.UNTRUSTED_EXTERNAL

    channels = config.get("channels", {}) or {}
    kinds = config.get("kinds", {}) or {}

    name = None
    channel = provenance.get("channel")
    if channel is not None:
        name = channels.get(str(channel))
    if name is None:
        kind = provenance.get("kind")
        if kind is not None:
            name = kinds.get(str(kind))

    if name is None:
        return Tier.UNTRUSTED_EXTERNAL
    return TIER_BY_NAME.get(str(name).lower(), Tier.UNTRUSTED_EXTERNAL)
