"""U6: the always-gate list — force approval regardless of tier or leg count.

An action class on the always-gate list always requires approval. Sends are
additionally gated when the recipient is not in the known-recipients list
(known-recipients.toml), which is itself a protected config (R8/R10) so a tool
call cannot pre-seed it to dodge the gate.
"""

from __future__ import annotations


def known_recipients(config: dict) -> set[str]:
    return set(config.get("recipients", []) or [])


def _extract_recipients(tool_input: dict, fields) -> list[str]:
    out = []
    for field in fields:
        val = tool_input.get(field)
        if isinstance(val, str):
            out.append(val)
        elif isinstance(val, (list, tuple)):
            out.extend(str(v) for v in val)
    return out


def is_always_gated(
    action_class_name, tool_name, tool_input, ag_config, kr_config, has_external_send=False
):
    """Return (gated: bool, reason: str|None).

    The new-recipient check fires when the action class is recipient-gated OR the
    call carries the external-send leg — so a send tool with no classes_of mapping
    still can't reach a new recipient without approval.
    """
    listed = set(ag_config.get("classes", []) or [])
    if action_class_name in listed:
        return True, f"always-gate: action class '{action_class_name}' requires approval"

    send_classes = set(ag_config.get("recipient_gated_classes", []) or [])
    if action_class_name in send_classes or has_external_send:
        fields = ag_config.get("recipient_fields", ["to", "recipient", "recipients"])
        known = known_recipients(kr_config)
        for recip in _extract_recipients(tool_input or {}, fields):
            if recip not in known:
                return True, f"always-gate: send to new recipient '{recip}' requires approval"
    return False, None
