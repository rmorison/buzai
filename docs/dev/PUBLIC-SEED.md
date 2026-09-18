# PUBLIC-SEED — publication policy for this repo

Living policy for what may appear in this public repo and what the leak gate
protects. Everything ships except PII/personal-infra details and the files
explicitly marked below. Enforced by `scripts/publish_gate.py` (pre-commit hook +
CI backstop) and gitleaks.

## Scrub rule categories

These are *categories*, deliberately generic. The literal patterns (actual hostnames,
account handles, box identifiers) live in a **private local gate-pattern file outside
this repo** — default `~/.config/buzai/gate-patterns`, consumed by
`scripts/publish_gate.py` when present — never commit literal sensitive strings to
this file or anywhere in the tree.

1. **Personal hostnames / box identifiers** — any reference identifying the
   maintainer's real machines or domains (except intentionally public branding sites).
2. **Account/user handles tied to real infrastructure** — SSH users, service accounts
   on real boxes.
3. **Emails** — except public contact addresses deliberately published.
4. **Private working-notes artifacts** — session-handoff files and similar; reference
   them as "(session handoff notes, private archive)".
5. **Credentials of any shape** — never present; enforced by gitleaks in pre-commit/CI
   regardless of this list.

## Standing rules

| Content | Rule |
|---|---|
| Product code, docs, engineering history (plans, brainstorms, solutions, decisions) | ship — write public-safe at authoring time |
| Personal state (`audit/`, `.buzai/`, `hubs/*`, `deploy/env/*.env`, `notes/`, `.claude/settings.local.json`) | never (gitignored) |
| The private gate-pattern file | **never in any repo** — lives on the maintainer's machine only |
| Workflows with write-scoped tokens or repo secrets in triggers | never without a security review |

## Authoring note

Development happens in the open. Write plans, brainstorms, and solution docs
public-safe from the start: no real hostnames or box identifiers (say "the test
box"), no personal contact details, no private-notes references. The pre-commit
gate catches known patterns; it cannot catch a novel sensitive detail the first
time — that's an authoring habit, not a tool guarantee.
