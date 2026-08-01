"""U7 (redaction half): reduce sensitive values to type + reference + hash.

v1 baseline, applied so the protected payload never enters the audit log (KTD7):
  - secret-bearing keys (authorization, token, password, ...) → fully redacted
  - free-text content fields (body, content, subject, message, ...) → reduced to
    {length, hash}; their prose is never logged verbatim
  - credential-shaped strings, SSNs, and credit-card numbers anywhere → redacted
  - email addresses → kept as domain-only reference (local part dropped)
  - file paths under a path-ish key → reduced to basename + hash
Unknown shapes under a sensitive key fail closed (fully redacted). The baseline is
deliberately conservative for v1; extend the key sets / patterns as needed.
"""

from __future__ import annotations

import hashlib
import re

CRED_RE = re.compile(
    r"(?i)\b(sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|[A-Za-z0-9_\-]{32,})\b"
)
EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

SECRET_KEYS = {
    "authorization",
    "auth",
    "token",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "key",
    "cookie",
    "set-cookie",
    "credential",
    "credentials",
    "env",
}
# Free-text fields whose prose must not be logged verbatim — reduced to a
# length+hash reference so the audit entry proves *that* content existed without
# exposing it.
CONTENT_KEYS = {
    "body",
    "content",
    "text",
    "message",
    "subject",
    "html",
    "snippet",
    "description",
    "note",
    "notes",
    "comment",
    "prompt",
}
PATH_KEYS = {"path", "file", "filename", "filepath", "file_path", "src", "dst", "attachment"}


def _hash(value) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _ref(kind, value, ref=None):
    out = {"_redacted": kind, "hash": _hash(value)}
    if ref is not None:
        out["ref"] = ref
    return out


def _redact_string(s: str):
    if CRED_RE.search(s):
        return _ref("credential", s)
    if SSN_RE.search(s) or CC_RE.search(s):
        return _ref("pii", s)
    m = EMAIL_RE.search(s)
    if m:
        return _ref("email", s, ref=m.group(2))  # domain only; local part dropped
    return s


def _basename(path: str) -> str:
    return re.split(r"[\\/]", path.rstrip("/\\"))[-1] or path


def redact(value, _key=None):
    if isinstance(value, dict):
        return {k: redact(v, _key=str(k).lower()) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, _key=_key) for v in value]
    if _key in SECRET_KEYS:
        # fail closed: never echo a value living under a secret-bearing key
        return _ref("secret-field", value)
    if _key in CONTENT_KEYS:
        return {"_redacted": "content", "chars": len(str(value)), "hash": _hash(value)}
    if isinstance(value, str):
        if _key in PATH_KEYS:
            return _ref("path", value, ref=_basename(value))
        return _redact_string(value)
    return value


# --- Audit-entry redaction (field-role aware) -------------------------------
# An audit entry mixes two very different things: fields the GATE itself authored
# (its verdict — tool, action_class, decision, tier, legs, reason) and the
# external payload it judged (tool_input). The payload redactor above is built
# for untrusted payload; applying it to the whole entry hashes the gate's own
# verdict — e.g. a managed-connector tool name like
# mcp__claude_ai_Google_Calendar__list_events is 32+ word-chars and trips the
# opaque-token branch of CRED_RE, so the log can't even show which tool was gated.
# Redact by field role instead: verdict stays legible (the point of the log),
# tool_input gets the full untrusted treatment.
_VERBATIM_ENTRY_KEYS = {
    "ts",
    "tool",
    "action_class",
    "decision",
    "tier",
    "legs",
    "trifecta_warning",
}


def _redact_reason(value):
    """Gate-authored reason text: keep it legible (it names the tool/policy), but
    scrub embedded PII — a new-recipient reason can carry an email address. Unlike
    the payload redactor, do NOT apply the opaque-credential catch-all here: it
    would match long MCP tool names embedded in the reason and hash the whole line.
    """
    if not isinstance(value, str):
        return redact(value)
    s = SSN_RE.sub("<ssn>", value)
    s = CC_RE.sub("<cc>", s)
    s = EMAIL_RE.sub(lambda m: m.group(2), s)  # drop local part, keep domain
    return s


def redact_entry(entry):
    """Redact a gate audit entry by field role: gate-authored verdict fields stay
    legible, `session` becomes a stable hash reference, `reason` keeps its text but
    scrubs embedded PII, and everything else (notably `tool_input`) gets the full
    untrusted-payload redaction.
    """
    if not isinstance(entry, dict):
        return redact(entry)
    out = {}
    for k, v in entry.items():
        kl = str(k).lower()
        if kl in _VERBATIM_ENTRY_KEYS:
            out[k] = v
        elif kl == "session":
            out[k] = _ref("session", v)
        elif kl == "reason":
            out[k] = _redact_reason(v)
        else:
            out[k] = redact(v)
    return out
