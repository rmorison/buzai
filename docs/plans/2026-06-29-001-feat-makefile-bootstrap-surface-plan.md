---
title: "feat: Makefile bootstrap/setup front door"
type: feat
status: active
date: 2026-06-29
---

# feat: Makefile bootstrap/setup front door

> **Historical note (2026-07):** this plan predates the single-repo restructure — it
> references a docs-overlay Makefile split and an export/validation step that no longer
> exist (the repo previously kept a private development tree separate from its published
> form). The verb surface and helper scripts it defines are current; the packaging
> details are history.

## Overview

Replace buzai's prose-driven onboarding with a Makefile that is the executable spine of
setup. Today an adopter follows `docs/user/docs/SETUP.md`'s ten steps by copy-pasting raw
command sequences; the brittle steps (pinning a venv, merging trust-gate hooks, validating
connector classification, installing the systemd unit, health-checking) are spelled out as
prose the reader must execute carefully and correctly.

The Makefile becomes a discoverable verb list (`make help`) that carries the commands; the
docs keep the *why* and the irreducibly-interactive answers. This is a **task runner / front
door**, not a build system — most targets are `.PHONY`. Two genuinely new helper scripts
(`install_hooks.py`, `trust_check.py`) convert the two most error-prone "hand it to your
claude session as a prompt" steps into deterministic, checked commands. A new `make doctor`
pulls in the health-aggregation that SETUP §Scope currently lists as **deferred**.

---

## Problem Frame

SETUP.md is a high-quality but dense ten-step runbook. The hardest steps for an adopter are
exactly the ones a script does better than prose:

- **Step 5a (venv):** prose tells you to install `uv`, create a pinned `.venv`, and run two
  unittest modules. A fresh box shipping system Python < 3.11 is a documented footgun.
- **Step 5b (wire the gate):** the runbook says to *hand the hook-merge to your claude
  session as a prompt* — a non-deterministic step that edits `.claude/settings.json` and
  `chmod`s `audit/`. Brittle and hard to verify.
- **Step 4/5 (classify connectors):** "uncomment the block and fix the `match` patterns" —
  a mislabel is a security hole, and nothing checks it.
- **Step 6 (service):** copy a unit template, `daemon-reload`, `enable --now`, `enable-linger`.
- **Step 8 (liveness/diagnosis):** scattered scripts (`liveness.py`, `secrets_preflight.py`)
  the adopter must know to invoke; §Scope explicitly defers an "assistant doctor" aggregate.

A Makefile names each of these as a verb, makes the verifiable ones deterministic, and leaves
the genuinely-interactive ones (`auth`, `prime-consent`) as hosted passthroughs with the prose
still carrying the answers.

---

## Requirements Trace

- R1. A single `make help` front door enumerates every setup/operate verb, self-documented.
- R2. The verifiable build steps are deterministic targets wrapping the existing scripts:
  `venv`, `test`, `liveness`, `smoke`, `env-url`, `service-{install,start,stop,restart,status}`.
- R3. `make doctor` aggregates prereqs + venv-present + liveness + secrets-preflight + auth/
  version into one health summary (the previously-deferred "assistant doctor").
- R4. `make trust-install` deterministically merges the trust-gate hooks into
  `.claude/settings.json` (non-destructive, idempotent) and sets `audit/` to 0700.
- R5. `make trust-check` validates that the trust-gate config parses and the gate self-test
  passes (v1); diffing classification coverage against live MCP tools is deferred (v2).
- R6. Interactive steps (`auth`, `prime-consent`) are hosted as passthrough targets; the docs
  still carry the required interactive answers.
- R7. Dev-only verbs (`lint`) are tagged `[dev]` and kept apart from the adopter-facing
  verb surface. *(Historical: originally enforced via a separate unshipped Makefile.)*
- R8. SETUP.md (and the raw-command parts of TROUBLESHOOTING.md) are rewritten to drive
  through `make`, with prose reduced to rationale + interactive answers; §Scope updated so
  `doctor` is IN, not deferred.

---

## Scope Boundaries

- **Not a build system.** No file-target dependency DAG beyond `.venv` as a guard; targets are
  `.PHONY` task-runner verbs. Do not contort interactive steps to fit a non-interactive model.
- **No Docker.** Explicitly deferred earlier; the Makefile will simply wrap it if/when it lands.
- **No new functional behavior in the trust gate, liveness, or service model.** This work wraps
  and lightly aggregates existing, tested machinery; it does not change gate semantics.
- `trust-check` **v2** (introspect `claude mcp list` and diff against `connector-legs.toml`) is
  out — v1 validates config parse + gate self-test only.
- `make auth` / `make prime-consent` do **not** attempt to automate the interactive prompts —
  they drop the user into the interactive command.

### Deferred to Follow-Up Work

- `trust-check` v2 (live MCP coverage diff).
- Promoting `make doctor`'s recipe into a standalone `scripts/doctor.py` if the recipe grows.

---

## Context & Research

### Relevant Code and Patterns

- `docs/user/docs/SETUP.md` — the ten-step runbook this work restructures. Steps map to verbs
  as: 5a→`venv`/`test`, 5b→`trust-install`, 4/5→`trust-check`, 6→`service-*`/`prime-consent`,
  8→`liveness`/`doctor`, 10→`smoke`, 3→`auth`, 7→`env-url`.
- `scripts/liveness.py`, `scripts/secrets_preflight.py`, `scripts/smoke_test.py`,
  `scripts/env_url.py` — all expose `def main(argv=None) -> int` and `raise SystemExit(main())`;
  invoked as `.venv/bin/python scripts/<name>.py`. Targets wrap these verbatim.
- `trust/settings.example.json` — the hooks to merge: a `hooks` object with `UserPromptSubmit`
  (`trust/bin/mark-turn`) and `PreToolUse` matcher `"*"` (`trust/bin/gate`). The `_comment`
  key documents the merge contract `install_hooks.py` implements.
- `deploy/claude-remote.service.template` — uses systemd `%h` specifiers and hardcodes
  `WorkingDirectory=%h/buzai` and `--name buzai-assistant`; `service-install` copies it to
  `~/.config/systemd/user/claude-remote.service`, with `sed` substitution only when `WORKDIR`/
  `NAME` differ from the defaults.
- `trust/config/*.toml` (connector-legs, tiers, policy) — parsed by `trust-check` via `tomllib`
  (stdlib in 3.12; the pinned `.python-version` is 3.12).
- `trust/tests/test_units.py`, `trust/tests/test_gate.py` — the 39-test suite `make test` runs;
  `trust-check` reuses a subset (or the whole suite) as its gate self-test.

### Institutional Learnings

- `docs/solutions/enforcing-lethal-trifecta-without-self-bricking-2026-06-28.md` and
  `connector-classification-as-security-2026-06-28.md` — why a connector mislabel is a security
  hole; informs the conservative, informational-not-silent behavior of `trust-check`.
- `docs/solutions/running-claude-code-always-on.md` — the consent-priming + auth-mode traps the
  `prime-consent`/`auth` passthrough targets and `doctor` must surface, not paper over.

### External References

- None needed — GNU Make `include`, `.PHONY`, and the `##`-comment self-documenting `help`
  idiom are well-established; no version-specific research warranted.

---

## Key Technical Decisions

- **One adopter-facing Makefile; dev-only verbs kept visibly separate (`[dev]` tag).**
  The durable decision is the split between the adopter verb surface and maintainer
  tooling. *(Historical: originally implemented as two files — a shipped Makefile plus an
  unshipped dev-root include — to serve the since-retired packaging layout; later merged
  into the single root Makefile.)*
- **`help` is the default goal, self-documented via a `## ` trailing-comment convention.** Each
  target's doc string lives on its rule line; a single awk/grep `help` recipe prints them. No
  second source of truth to drift.
- **New helper scripts over fat Makefile recipes for the two stateful steps.** JSON deep-merge
  (`install_hooks.py`) and TOML/gate validation (`trust_check.py`) are real logic that deserves
  unit tests; a shell recipe would be untestable and fragile. They live in `scripts/` (already
  shipped) and run under `.venv/bin/python`.
- **`doctor` stays a Makefile recipe orchestrating already-tested scripts**, not a new script —
  it mostly shells to `systemctl`/`ss`/`claude` which aren't unit-testable. Promotion to
  `scripts/doctor.py` is deferred until/unless the recipe grows.
- **Overridable vars:** `NAME ?= buzai-assistant`, `WORKDIR ?= $(HOME)/buzai`, `PY := .venv/bin/python`.
- **`trust-install` is non-destructive and idempotent.** It deep-merges (never overwrites) and
  is safe to re-run — the single most important property, since it edits a user's existing
  `.claude/settings.json`.

---

## Open Questions

### Resolved During Planning

- *Where does the shipped Makefile live?* `docs/user/Makefile` (overlays to eval root), like
  `README.md` already does — `docs/user/` is the eval overlay root, not literal docs.
- *Script vs recipe for doctor?* Recipe (orchestration of tested scripts); script for the two
  stateful steps (merge, validate).
- *TOML parsing dependency?* None — `tomllib` is stdlib on the pinned 3.12.

### Deferred to Implementation

- Exact `sed` substitution for `service-install` when `WORKDIR`/`NAME` differ from template
  defaults — finalize against the real template specifiers during implementation.
- Whether `trust-check`'s gate self-test runs the full 39-test suite or a fast subset — decide
  when wiring it; the full suite is the safe default if it's quick.
- The precise deep-merge semantics for a pre-existing `PreToolUse` array that already contains a
  non-buzai hook (append vs. dedup-by-command) — resolve against a real settings.json shape.

---

## Implementation Units

- U1. **Shipped Makefile — `help` + build/health/service/interactive verbs**

**Goal:** Author `docs/user/Makefile` as the front door: `help` (default goal) plus the targets
that wrap existing scripts and lifecycle commands. This is the spine the later units plug into.

**Requirements:** R1, R2, R3, R6

**Dependencies:** None (U2/U3 add their targets onto this file; `doctor` in U1 can reference
`trust-check` as an optional sub-check once U3 lands, or omit it initially).

**Files:**
- Create: `docs/user/Makefile`

**Approach:**
- `.PHONY` everything except a `.venv` guard. Vars: `NAME ?= buzai-assistant`,
  `WORKDIR ?= $(HOME)/buzai`, `PY := .venv/bin/python`.
- Targets implemented here (scripts already exist): `venv` (uv install + `uv venv --python 3.12
  .venv`), `test` (`$(PY) -m unittest trust.tests.test_units trust.tests.test_gate`), `liveness`
  (`$(PY) scripts/liveness.py`), `smoke` (`$(PY) scripts/smoke_test.py`), `env-url`
  (`$(PY) scripts/env_url.py --force`), `service-install` (mkdir + cp template + optional sed for
  NAME/WORKDIR + `systemctl --user daemon-reload`), `service-start|stop|restart|status`
  (`systemctl --user … claude-remote`), `auth` (drops into `claude`), `prime-consent` (drops into
  `claude remote-control --name $(NAME)`), `doctor` (recipe: prereqs check → venv present →
  `liveness.py` → `secrets_preflight.py` → `claude --version`/auth note; non-zero only on hard
  failures).
- A header comment documents the task-runner-not-build-system intent and the load-bearing
  layout-symmetry that lets the dev-root include work.

**Patterns to follow:**
- Existing `.venv/bin/python scripts/<name>.py` invocation from SETUP.md.
- The `%h`/`WorkingDirectory=%h/buzai`/`--name buzai-assistant` shape of
  `deploy/claude-remote.service.template` for the `service-install` cp/sed.

**Test scenarios:**
- Happy path: `make help` prints every documented target with its `## ` description.
- Happy path: `make -n liveness` / `make -n test` / `make -n smoke` expand to the correct
  `.venv/bin/python scripts/…` commands (dry-run, no execution).
- Edge case: `make -n service-install NAME=other WORKDIR=/srv/buzai` shows the unit copied and
  the substitution applied.
- Test expectation: behavioral coverage is limited to `make -n` dry-run + `make help` parsing;
  the recipes themselves wrap already-tested scripts or non-unit-testable system commands.

**Verification:**
- `make help` lists all verbs; dry-runs resolve to the intended commands; `make liveness`/`test`
  produce the same output as invoking the scripts directly.

---

- U2. **`scripts/install_hooks.py` + `trust-install` target**

**Goal:** Deterministically, non-destructively, idempotently merge the trust-gate hooks from
`trust/settings.example.json` into the workspace `.claude/settings.json`, and set `audit/` to
0700 — replacing the "hand it to your claude session as a prompt" step.

**Requirements:** R4

**Dependencies:** U1 (adds the `trust-install` target to the Makefile)

**Files:**
- Create: `scripts/install_hooks.py`
- Create: `scripts/tests/test_install_hooks.py`
- Modify: `docs/user/Makefile` (add `trust-install` target → `$(PY) scripts/install_hooks.py`
  then `mkdir -p audit && chmod 700 audit`)

**Approach:**
- Read `trust/settings.example.json`'s `hooks` object; deep-merge into an existing
  `.claude/settings.json` (create it if absent), preserving unrelated keys. Append the buzai
  `PreToolUse`/`UserPromptSubmit` entries only if an entry with the same `command` is not already
  present (idempotent). Never overwrite a malformed existing file — fail loudly.
- `chmod 700 audit` can stay in the Makefile recipe (simple, observable) rather than the script.

**Patterns to follow:**
- The merge contract documented in `trust/settings.example.json`'s `_comment` (merge, don't
  overwrite; both hooks run under `.venv` python).

**Test scenarios:**
- Happy path: no existing `.claude/settings.json` → file created with both hook groups.
- Happy path: existing settings.json with an unrelated key (e.g. `"model"`) → hooks added, the
  unrelated key preserved.
- Edge case (idempotent): running twice does not duplicate the `PreToolUse`/`UserPromptSubmit`
  entries.
- Edge case: existing `PreToolUse` array already holds a *different* (non-buzai) hook → buzai
  gate appended without clobbering the existing entry.
- Error path: existing settings.json is invalid JSON → non-zero exit, clear message, original
  file left untouched.

**Verification:**
- After `make trust-install` on a fresh clone, `.claude/settings.json` contains both hooks and
  `audit/` is mode 0700; a second run is a no-op.

---

- U3. **`scripts/trust_check.py` + `trust-check` target (v1)**

**Goal:** Validate that the trust-gate configuration parses and the gate self-test passes, and
report (informationally, not fatally) any connector tool that classifies to zero legs —
converting "carefully uncomment and fix match patterns" into a checked step.

**Requirements:** R5

**Dependencies:** U1 (adds the `trust-check` target)

**Files:**
- Create: `scripts/trust_check.py`
- Create: `scripts/tests/test_trust_check.py`
- Modify: `docs/user/Makefile` (add `trust-check` → `$(PY) scripts/trust_check.py`)

**Approach:**
- Parse `trust/config/connector-legs.toml`, `tiers.toml`, `policy.toml` with `tomllib`; any parse
  error is a hard, file-named failure.
- Run the gate self-test (the trust unit/gate suite, or a fast subset) and surface its result.
- Walk classified connector tools; a tool resolving to zero legs is reported as "gates by *ask*
  (unclassified)" — informational, exit 0 — matching the documented conservative default.
- v2 (diff against live `claude mcp list`) is explicitly out.

**Patterns to follow:**
- `connector-classification-as-security` learning — conservative, never silently pass a mislabel.
- The existing unittest invocation used by `make test` for the self-test sub-step.

**Test scenarios:**
- Happy path: well-formed config + green self-test → exit 0, "OK" summary.
- Error path: malformed `connector-legs.toml` → non-zero exit naming the file.
- Edge case: a connector tool present with no legs → reported as unclassified/ask, still exit 0.
- Integration: the self-test sub-step actually executes the gate suite (not mocked) and a failing
  gate test propagates to a non-zero `trust-check` exit.

**Verification:**
- `make trust-check` on the shipped config exits 0 with a coverage summary; corrupting a TOML
  file makes it fail and name the file.

---

- U4. **Dev-root Makefile (unshipped) — include + dev-only verbs**

*(Historical unit: created a separate maintainer-only Makefile for the since-retired
packaging layout. Superseded — dev verbs now live in the single root Makefile, tagged
`[dev]`.)*

---

- U5. **Rewrite SETUP.md (and thin TROUBLESHOOTING.md) to drive through `make`**

**Goal:** Make the docs carry rationale + interactive answers while `make` carries the commands;
update §Scope so `doctor` is IN.

**Requirements:** R8

**Dependencies:** U1, U2, U3 (the verbs must exist before the docs point at them)

**Files:**
- Modify: `docs/user/docs/SETUP.md`
- Modify: `docs/user/docs/TROUBLESHOOTING.md`

**Approach:**
- For each SETUP step with a target, replace the raw command block with `make <verb>` plus the
  retained *why* and (for `auth`/`prime-consent`) the exact interactive answers. Keep doc-only
  steps (host bootstrap, clone, managed-connector web OAuth) as prose.
- Update §Scope: remove "an `assistant doctor` automation (this runbook documents the manual
  checks instead)" from the deferred list (now `make doctor`).
- In TROUBLESHOOTING.md, replace raw `liveness.py`/`ss`/`systemctl` recipes with `make
  liveness`/`make doctor`/`make service-restart` where a verb exists; keep the diagnostic prose.

**Patterns to follow:**
- The existing candor/voice of SETUP.md and TROUBLESHOOTING.md (keep it).

**Test scenarios:**
- Test expectation: none (documentation). Verified by review + the U6 `--check` gate.

**Verification:**
- Every `make` verb referenced in the rewritten docs exists in `docs/user/Makefile`; §Scope no
  longer lists the doctor automation as deferred.

---

- U6. **Sync-gate validation pass**

*(Historical unit: validated the since-retired export step. The equivalent standing
check today is `scripts/publish_gate.py` + the pre-commit hooks.)*

---

## System-Wide Impact

- **Interaction graph:** changing target names ripples into SETUP.md/TROUBLESHOOTING.md
  references — verb names are load-bearing across the docs.
- **API surface parity:** the Makefile *is* a new external contract surface for adopters — verb
  names are now public API. Keep them stable once shipped.
- **State lifecycle risks:** `trust-install` edits a user's existing `.claude/settings.json`;
  non-destructive idempotent merge is the critical invariant (U2 test scenarios enforce it).
- **Unchanged invariants:** trust-gate semantics, liveness/health logic, and the systemd service
  model are unchanged — this work wraps and aggregates them; it does not alter gate decisions,
  the audit log, or the service's runtime behavior.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `trust-install` corrupts a user's existing `.claude/settings.json` | Non-destructive deep-merge, idempotent, fail-loud on malformed JSON, never overwrite (U2 tests). |
| Verb names churn after adopters depend on them | Treat verb names as public API; finalize in U1 before docs reference them. |
| `make doctor` masks the real interactive-auth/consent traps | `doctor` reports auth-mode and registration state plainly (surfacing, not papering over) per the always-on learnings. |
| Scope creep into a real build system / Docker | Plan caps targets at `.PHONY` task-runner verbs; Docker explicitly deferred. |

---

## Documentation / Operational Notes

- SETUP.md and TROUBLESHOOTING.md are rewritten as part of U5 (not a follow-up) — the doc change
  is the point, not a side effect.

---

## Sources & References

- Runbook restructured: `docs/user/docs/SETUP.md`
- Scripts wrapped: `scripts/liveness.py`, `scripts/secrets_preflight.py`, `scripts/smoke_test.py`,
  `scripts/env_url.py`
- Merge contract: `trust/settings.example.json`; service template: `deploy/claude-remote.service.template`
- Learnings: `docs/solutions/connector-classification-as-security-2026-06-28.md`,
  `docs/solutions/running-claude-code-always-on.md`
