---
title: A preflight that checks permissions has not checked usability
date: 2026-09-18
category: best-practices
module: deploy / secrets preflight, auth durability
problem_type: best_practice
component: assistant
severity: high
related_components:
  - authentication
  - deployment
  - tooling
applies_when:
  - Writing a preflight or health check over a credential or config file
  - Deciding what "this file is fine" means for a file the service depends on
  - Diagnosing a service that starts, stays active, and never becomes useful
  - Reporting on a secret without echoing it
tags:
  - preflight
  - credentials
  - auth-durability
  - observability
  - fail-closed
---

# A preflight that checks permissions has not checked usability

## Context

The secrets preflight verified that `~/.claude/.credentials.json` was mode 0600 and not
tracked by git. Both true, both important, and both answering the same question: *is this
file safe?*

On 2026-09-02 a live instance went down and stayed down for nine days. The stored OAuth
tokens had been emptied in place — `accessToken` and `refreshToken` were both the empty
string. The file was still present. Still 0600. Still untracked. The preflight passed on
every start, exited 0, and the service then failed at `ExecStart` with *"You must be
logged in to use Remote Control"*, five times inside five minutes, until `StartLimitBurst`
stopped systemd retrying.

Every check that existed was green. None of them asked the other question: *does this file
still work?*

## Guidance

**Name the question each check answers.** "Permissions are correct" and "the contents are
usable" are different properties of the same file, and passing the first says nothing
about the second. Write them down separately, because a reader — and a future you at
3 a.m. — will otherwise read a green preflight as "the credential is fine".

**Check content where the failure mode is content.** A credential's failure mode is
revocation and expiry, not chmod. If the thing that actually goes wrong in production is
"the token stopped working", then a check that cannot observe that is not a check of the
credential; it is a check of the filesystem.

**Stay silent on shapes you do not recognise.** Warn when the structure is present and
demonstrably empty; say nothing when the file has an auth shape the check does not know.
A confident wrong warning is worse than no warning: it teaches the operator to ignore the
channel, and the next warning is the real one.

**Never echo the secret while reporting its absence.** Report that a token is missing, not
what it was — and test that. The value is exactly what must not reach a journal that this
design already sends to disk.

**Keep it a warning.** A logged-out instance already fails at `ExecStart`; making the
preflight fatal adds a second way to die, and the fatal path is the one that stops systemd
retrying at all. The value of the check is not that it blocks — it is that the journal and
`make doctor` can *name the cause* instead of leaving an operator to infer it from a
restart loop.

## Why This Matters

The nine days are the lesson. Nothing was broken in a way anyone could see: the unit
existed, the config was valid, the file was present and correctly permissioned, and the
one check that ran said OK. The gap between "every check passes" and "the system works"
was invisible precisely because the checks were about a different property than the
failure.

This generalises past credentials. Any precondition you verify structurally — a config
file that parses, a socket that exists, a directory that is present, a certificate that is
readable — can be structurally perfect and semantically dead. When you write a check, say
out loud which failure it would have caught, and then ask which real outage it would not
have.

## When to Apply

- Any preflight or health check over a credential, token, or API key.
- Any check whose passing condition is a file's *metadata* while the dependency is on its
  *contents*.
- Any always-on service where "active" and "working" are not the same state — and where
  nobody is watching the difference.
- Post-incident: for each check that was green during an outage, ask what property it
  actually asserted.

## Examples

The check that closes the gap. It is deliberately narrow — it fires on a recognised shape
with no usable token, and on nothing it does not understand:

```python
# scripts/secrets_preflight.py
def credential_warning(text: str | None, creds: Path) -> str | None:
    """Whether the stored Claude credentials can still authenticate. Pure.

    The perms check above asks whether this file is *safe*; this asks whether it still
    *works*. They are different questions, and only the first was ever asked.
    """
    ...
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        # An auth shape this does not recognize. Saying nothing beats guessing.
        return None
    if any(str(oauth.get(k) or "").strip() for k in ("accessToken", "refreshToken")):
        return None
    return (
        f"{creds} holds no usable token — accessToken and refreshToken are both empty, "
        f"so the service will start but never register. ... run `make auth`"
    )
```

Pinned against the outage's exact on-disk shape, and against leaking the value it reports:

```python
def test_the_emptied_token_shape_that_caused_the_outage_is_named(self):
    w = self.warn(self.oauth(accessToken="", refreshToken=""))
    self.assertIsNotNone(w)

def test_no_warning_ever_carries_a_token_value(self):
    secret = "sk-ant-oat01-DO-NOT-ECHO"
    for text in (self.oauth(accessToken=secret, refreshToken=""), "{" + secret):
        self.assertNotIn(secret, self.warn(text) or "")
```

And a warning, never fatal — asserted through the real composition rather than trusted:

```python
def test_emptied_credentials_warn_through_assess_and_never_block_start(self):
    ...
    self.assertEqual(findings.fatal, ())
    self.assertFalse(findings.blocks_start)
```

## Related

- [A start-time gate needs a severity split](../architecture-patterns/start-time-gate-severity-split-2026-09-18.md) — why this is a warning and not a fatal, and what `StartLimitBurst` does to the alternative.
- [Running Claude Code always-on](../architecture-patterns/running-claude-code-always-on.md) — §4 documents the token *lifetime* failure mode (idle ceiling, no headless re-auth); this is the *emptied-in-place* one it did not cover, and §5's "a guard only protects you if it receives inputs that can actually fail it" is the same principle applied to a different input.
- Origin: the 2026-09-02 outage on a live instance, diagnosed during the fresh-install smoke test of the private-versioned-hubs work.
