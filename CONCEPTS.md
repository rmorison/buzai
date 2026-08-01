# Concepts

Shared domain vocabulary for buzai — entities, named processes, and status concepts with
project-specific meaning. A glossary to consult while reading the other docs, not a spec
or catch-all.

## The instance and its principal

### Principal
The single human a buzai instance serves. Each instance serves exactly one
principal — there is no multi-tenant mode — so "the principal" is unambiguous within
an instance, and trust, hubs, and connectors are all scoped to that one person.

### Hub
A personal knowledge-base document that is the principal's source of truth for one
domain (e.g. finances, a project, a relationship). Hubs are the principal's curated
state; the template ships only empty scaffolds, and real hubs stay out of version
control.

## Remote Control and liveness

### Remote Control
The mechanism by which the Claude desktop/mobile apps attach to a running headless
Claude Code instance. The instance runs a long-lived Remote Control server; the apps
connect to it through a relay rather than directly.

### Environment URL
The relay address the apps reconnect to. A new one is minted on every (re)start of
the server, so the previous URL is dead after a restart and there is no command that
reports the current one — it must be recovered from the service logs. It grants
owner-equivalent access and must never be exposed to logs, chat, or monitoring.
*Avoid:* reconnect URL.

### Last-good-turn
The most recent completed agent turn, read from the logs and used as the liveness
signal. Because a headless instance can't be cheaply asked "are you alive?", a recent
last-good-turn stands in for a round-trip; it — together with an established relay
socket — is what "live" means, not the startup "Ready" banner.

## Connectors

### Managed connector
A claude.ai-hosted integration (Gmail, Calendar, Drive, Todoist) that loads only
under the Claude.ai-subscription auth method, never under an API key. Its OAuth is
established once attended and then persists; there is no headless re-auth flow.

### Local MCP server
An MCP server the principal runs themselves (e.g. Sheets, Xero) and registers locally,
as opposed to a managed connector. It is not bound to the subscription auth method and
is configured per instance.

## Trust

### Trust gate
The PreToolUse-hook layer that assigns each tool call a channel-derived trust tier,
enforces the "Rule of Two," and writes a redacted, hash-chained audit log. It is the
instance's safety boundary between untrusted content and high-impact actions.

### Always-gate
The list of high-blast-radius actions that always require confirmation regardless of
trust tier — the standing exceptions the trust gate never auto-approves.

## Flagged ambiguities

- "Connector" alone is ambiguous: it spans both **Managed connector** and **Local MCP
  server**, which differ in auth method and where they're configured — name which one
  is meant.
