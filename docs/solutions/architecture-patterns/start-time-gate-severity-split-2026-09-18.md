---
title: A start-time gate needs a severity split, not just more checks
date: 2026-09-18
category: architecture-patterns
module: deploy / systemd ExecStartPre preflight
problem_type: architecture_pattern
component: assistant
severity: high
related_components:
  - deployment
  - tooling
  - development_workflow
applies_when:
  - Adding a check to an ExecStartPre/ExecStartPost gate on an always-on service
  - Choosing the exit code for a precondition that is absent rather than unsafe
  - Running the same binary both from a unit and by hand from a Makefile
  - Deciding what an operator should read in the journal to confirm a guarantee held
tags:
  - systemd
  - fail-closed
  - start-limit
  - preflight
  - always-on
  - observability
---

# A start-time gate needs a severity split, not just more checks

## Context

An always-on assistant runs as a `--user` unit with `Restart=always`, and its unit gates
start on a preflight: `ExecStartPre` runs a script that refuses to start the service on an
unsafe layout. The instinct when adding a check is to make it fail — that is what a gate
is for.

That instinct is wrong for half the checks, and the reason is `StartLimitBurst`. With
`StartLimitBurst=5` and `StartLimitIntervalSec=300`, five failed starts inside five
minutes make systemd stop trying **permanently**. A precondition that is merely *absent*
— no hub store yet, nothing backed up yet, no remote attached yet — is a condition that
will not resolve on its own between retries. Make it fatal and the gate converts "your
knowledge is not backed up" into "the assistant is gone, and will not come back on its
own". The safety mechanism becomes the outage.

The mirror-image mistake is quieter. Three of this service's start-path scripts printed
`FAIL:` for a state every fresh instance passes through — no hub store between `make
setup` and `make hub-init`. Nothing broke, because the unit's leading `-` absorbed the
exit code. But `FAIL:` was also the word reserved for *personal content is exposed*, and
printing it at every start on a routine state teaches an operator to skim past the one
string that must never be skimmed.

## Guidance

**Split by consequence, not by how serious the check feels.** Two tiers, and the test for
which tier a check belongs to is a single question: *if this condition persists, is data
exposed, or is it merely not yet protected?*

- **Leak conditions → FAIL, exit 1.** Personal content inside a public checkout, a secret
  with loose permissions, a tracked credential, a hub path that resolves inside the
  repository. Starting is worse than not starting.
- **Durability and absence conditions → WARN, exit 0.** No remote yet, an unpushed
  backlog, a dirty tree, no store yet, no usable login. Not starting is worse than
  starting.
- **Legitimate empty states → a plain note.** "No local secret files" on a
  managed-connector-only instance is not a warning at all; it is the expected shape.

**Give the two tiers different words, and mean them.** Reserve `FAIL:` for the tier that
blocks. If every state prints `FAIL:`, the word carries no information and the journal
stops being a place anyone looks.

**A service-only leniency needs a flag, not a global softening.** The same scripts run by
hand from the Makefile (`make hub-push`, `make hub-remote-check`). Softening "no store
yet" to exit 0 unconditionally made those owner-run commands report success on a store
that did not exist. The downgrade belongs behind a flag only the unit passes
(`--if-initialized`), so the service is lenient and the human's command is not.

**Prefer durable evidence to journal lines for anything you tell an operator to verify.**
A guarantee an operator is told to confirm by reading the journal is only as good as the
journal. On a real reboot of this instance, both `ExecStartPre` commands ran and exited 0
— systemd recorded `status=0` — and the privacy probe genuinely re-verified, but neither
command's stdout reached the journal on the boot path. Three of four starts logged it; the
boot did not. The verdict was still provable, because the probe writes `verified_at` into
a durable cache. Point health checks at that, not at log text.

## Why This Matters

This is the same failure family as a fail-closed safety gate that bricks the box: a
control that degrades into a liveness failure. But it has a nastier property. A gate that
denies a tool call fails loudly and immediately, in front of the person who asked. A gate
that fails *at start* fails when nobody is watching, and `StartLimitBurst` makes the
failure permanent rather than transient. The assistant is simply gone, and the next signal
is a human noticing days later.

The observability half matters for the same reason. An always-on service is one you are
not watching; the journal is the only account of whether its start-time guarantees held.
A guarantee that ran but left no trace is indistinguishable, to the operator, from one
that silently stopped running.

## When to Apply

- Any `ExecStartPre`/`ExecStartPost` gate on a unit with `Restart=always` — check the
  `StartLimitBurst` interaction **before** choosing an exit code.
- Any precondition that a fresh install passes through on its way to being configured.
- Any script that is run both by a unit and by a human; decide whether leniency is a
  property of the code or of the caller.
- Any health check or doc that tells an operator "confirm X in the logs" — prefer a
  durable artifact the check itself writes.

## Examples

The split, made explicit in the reporter so a test can assert it rather than a reader
having to trust it:

```python
# scripts/secrets_preflight.py
def report(target: HubTarget, findings: Findings) -> int:
    """Print the verdict and return the exit code. 1 only for leak conditions."""
    # Warnings are durability conditions and exit 0 by design. With StartLimitBurst=5,
    # failing here would turn "knowledge is not backed up" into "the assistant is gone".
    for warning in findings.warnings:
        print(f"secrets-preflight WARN: {warning}", file=sys.stderr)
    if findings.blocks_start:
        for problem in findings.fatal:
            print(f"secrets-preflight FAIL: {problem}", file=sys.stderr)
        return 1
```

Leniency scoped to the caller, not baked into the code — the unit passes the flag, the
Makefile does not:

```
ExecStartPre=-%h/buzai/.venv/bin/python %h/buzai/scripts/hub_remote.py --if-initialized
```

```python
# scripts/hub_remote.py — the same binary, run by hand, still fails
if not (location.path / ".git").exists():
    if args.if_initialized:
        print(f"hub-remote: {location.path} is not a hub repo yet — nothing to verify", ...)
        return 0
    print(f"hub-remote FAIL: {location.path} is not a hub repo yet — run `make hub-init`", ...)
    return 1
```

## Related

- [Enforcing the lethal trifecta without self-bricking the assistant](../design-patterns/enforcing-lethal-trifecta-without-self-bricking-2026-06-28.md) — the same shape one layer up: a fail-closed control that turns into a denial of service against its own owner.
- [Running Claude Code always-on](running-claude-code-always-on.md) — §5's "a guard only protects you if it receives inputs that can actually fail it", and §6's "active but silently broken" cluster.
- [A preflight that checks permissions has not checked usability](../best-practices/preflight-checks-content-not-just-permissions-2026-09-18.md) — the companion lesson about *what* a start-time check looks at.
- Origin: the fresh-install smoke test of the private-versioned-hubs work (PR #3); commits `c74c3d6` (the split), `b6deb6a`..`a814a92` (the flag), and the boot-path observability gap found after a real reboot.
