---
title: Verify a collaboration tool's real access model before trusting it with sensitive content
date: 2026-06-28
category: best-practices
module: collaboration tooling / Proof
problem_type: best_practice
component: tooling
severity: medium
related_components:
  - documentation
  - assistant
applies_when:
  - Adopting a third-party collaboration or document-sharing tool
  - Deciding whether to put sensitive content in an external editor
  - Reasoning about capability-by-link vs ACL access models
tags:
  - access-model
  - capability-by-link
  - proof
  - tooling
  - due-diligence
  - security
---

# Verify a collaboration tool's real access model before trusting it with sensitive content

## Context

When you put idea- or data-sensitive material into an external collaboration tool, you are trusting that tool's access model to match your mental model of it. A common, dangerous assumption is that a tool scopes documents to the user's account ACL — that "my docs are private to my account unless I share them." That assumption is frequently wrong, and the gap is invisible until you probe it empirically.

A specific instance: a collaborative markdown editor (Proof / proofeditor.ai) was assumed to scope documents to the user's account ACL. Empirical probing of its actual share/agent API showed a different model entirely.

## Guidance

**Probe the actual access model before trusting a tool with anything sensitive.** Do not infer the model from UI affordances, marketing, or the presence of an "owner" field. Test it directly — the highest-signal test is to fetch a document's content route **with no authentication token at all** and observe what you get back.

**Distinguish capability-by-link from ACL-by-account.** These are fundamentally different security models:

- *ACL-by-account*: access is checked against the requester's identity; an unshared doc is unreachable by others.
- *Capability-by-link*: possession of the URL (the slug) **is** the authorization. Anyone with the link reads the content; there is no identity check on read.

In the probed tool, the content route served the **full document to anyone with the slug, with no token (HTTP 200 verified)**. Ownership (a "claim" or `ownerId`) controlled delete, attribution, and Library placement — **not read access**. There was **no "private" visibility state** at all. The link is the credential.

**Default sensitive material to local, version-controlled storage.** Unless and until you have verified a tool enforces account-scoped read access, keep anything idea- or data-sensitive in local, version-controlled storage. Use the external collaboration tool for content you are comfortable being readable by anyone who obtains the link.

## Why This Matters

A wrong access-model assumption is a silent data-exposure path. There is no error, no warning, and the tool works exactly as advertised for the *sharing* use case — the failure is entirely in the gap between "I assumed private-by-account" and "it's actually public-by-link." For an assistant that handles personal data and proprietary ideas, mis-trusting a collaboration tool can leak the very material the rest of the trust system is built to protect. This pairs directly with connector classification: you cannot correctly classify a tool's legs if you have the tool's access semantics wrong.

## When to Apply

- Before sending any sensitive document, plan, draft, or dataset to a third-party collaboration, sharing, or "agent API" tool for the first time.
- When a tool offers "share by link" — assume capability-by-link until proven otherwise.
- During any security or trust review that involves external content storage.
- When onboarding a new MCP connector or integration that stores or exposes content.

## Examples

The empirical probe that settled it — fetch the content route with no token and check the status:

```bash
# Capability-by-link test: request the doc content route with NO auth header.
# A 200 with full content == the link is the credential (public-by-link),
# regardless of any "owner" or "private" UI affordance.
curl -s -o /dev/null -w '%{http_code}\n' "https://<tool-host>/d/<slug>"
# Observed: 200  (full document served, no token)
```

What the fields actually controlled, once probed:

- `/d/<slug>` content route → **read access by link possession, no token** (HTTP 200 verified).
- claim / `ownerId` → delete, attribution, Library placement only — **not read**.
- visibility states → **no "private" state exists**.

Decision rule that follows:

```text
Sensitive (ideas, personal/proprietary data) → local, version-controlled storage.
Shareable / link-is-fine                      → the external collaboration tool.
Unknown access model                          → treat as public-by-link until probed.
```

## Related

- [Connector classification as security](connector-classification-as-security-2026-06-28.md) — the same "verify, don't assume" discipline applied to a tool's read/send legs.
- [Enforcing the lethal trifecta without self-bricking](../design-patterns/enforcing-lethal-trifecta-without-self-bricking-2026-06-28.md) — a leaked sensitive doc undermines the private-data perimeter the gate enforces.
