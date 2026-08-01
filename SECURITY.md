# Security Policy

buzai is a security-positioned project — a trust-and-governance gate for always-on
Claude Code assistants. Reports about the gate failing to do what
[`docs/SECURITY-MODEL.md`](docs/SECURITY-MODEL.md) claims are treated as
vulnerabilities, not bugs.

## Reporting a vulnerability

**Please do not open a public issue for security problems.** Use GitHub's private
vulnerability reporting ("Report a vulnerability" under the repo's Security tab),
which reaches the maintainer privately. You'll get an acknowledgment within a few
days.

In scope, especially:

- Trust-gate bypasses: a tool call reaching a connector without a gate decision,
  tier confusion, trifecta (Rule-of-Two) evasion, always-gate circumvention
- Audit-log tampering that doesn't break the hash chain, or redaction failures that
  leak protected payload content into the log
- Privilege escalation from the dedicated service user
- Injection paths through the installer or make targets

Known, documented limitations (Bash blind spot, subagent hook-coverage gap, account
login as attach perimeter) are listed in SECURITY-MODEL's "Honest limits" — reports
that add *new* edges to that list are still very welcome.

## Supported versions

The `main` branch. There are no maintained release branches yet.
