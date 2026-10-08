---
title: Adopt the engineering-standards leak gate - Plan
type: chore
date: 2026-10-08
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
origin: https://github.com/rmorison/buzai/issues/19
---

# Adopt the engineering-standards leak gate - Plan

## Goal Capsule

- Objective: a buzai maintainer or contributor cannot commit or push one of their own private values from a dev checkout without being stopped. The same holds for a formatted personal financial number in a committed file. The refusal never echoes the value. A contributor with no value list is never blocked by one.
- Means: replace buzai's own gate with the engineering-standards gate copied whole from commit `3ad35c7`. buzai adds its own shape rules on top (KTD1, KTD2).
- Authority: issue #19 and its two lead comments, then this plan. Where they disagree, the issue comments win.
- Stop conditions: stop and comment on #19 if a copied file has to change in a way KTD1 does not cover. Do the same if a shape rule hits the tree on something other than a test fixture, or if a proof can only pass by reading a real value list.
- Execution profile: one pull request on `19-adopt-leak-gate`. It merges last in sprint 1, after #17. The worker builds after the operator signs off on #19. The operator merges.

## Product Contract

### Summary

Copy the engineering-standards gate files byte for byte from `3ad35c7`. Append buzai's shape rules to `.gitleaks.toml`. Wire the gate through the pre-commit framework at commit and push time, and delete `scripts/publish_gate.py`. Clean the existing tree so the gate's CI tree scan passes. Record every difference from the standard, with its reason, in `docs/dev/PUBLIC-SEED.md`.

### Problem Frame

buzai's gate came before the standard's gate and has fallen behind it. Nothing checks commit messages before a push. A refusal prints the matched private pattern, so the refusal leaks the value it found. The private list is a buzai-only copy of the maintainer's cross-repository list, so the two can drift. gitleaks is v8.21.2, and the standard pins 8.24.2.

Planning found a second problem. The old CI never scanned the tree for credentials: the gitleaks pre-commit hook scans staged changes only, and CI stages nothing. Over the tracked files on current `main`, the upstream rules report 35 findings. All of them are invented or non-personal:

- the project's own service user's home directory, written as an absolute path under the `/home` root, in `docs/HOST-BOOTSTRAP.md`, `docs/SETUP.md` and `trust/tests/test_gate.py`;
- placeholder user directories under the same root in `scripts/tests/`;
- fake GitHub tokens and API keys in `scripts/tests/test_hub_commit.py`, `scripts/tests/test_hub_remote.py` and `trust/tests/test_units.py`.

buzai's candidate rules add 6 more: 3 SSN hits on one test fixture, and 3 handoff-pattern hits in `test_publish_gate.py`. `leaks.yml` scans the whole tree, so it fails on day one unless those lines change.

This document describes those findings by shape and file only. It is scanned like any other file, so it must not quote a matching path or number.

### Requirements

- R1. A commit that stages a value from the declared list is refused. The output names the list line (`list line N` or `[private-value-N]`), never the value. A clean commit goes through.
- R2. A push whose commit message holds a listed value is refused before anything reaches the remote. The output names the reason.
- R3. With no list declared, commit and push succeed and print one line saying value rules were skipped.
- R4. Each financial-data shape rule refuses its fixture and has zero hits on the tree this pull request leaves.
- R5. CI runs `leaks.yml` with committed rules only, never value rules, alongside the existing checks. GitHub's own `actions/*` stay on the major tag, as upstream ships them, and any third-party action is pinned by SHA (lead refinement 2).
- R6. Nothing reads `~/.config/buzai/gate-patterns`. After merge, the operator takes these steps in order, and the pull request lists them:
  1. Confirm that every entry of the old file is in the `leakgate.values` list. The operator notes that the old file was generated from that list, so this is a confirmation, not a move.
  2. Re-run `make dev` in each existing clone, and confirm that its coverage line shows value rules active.
  3. Once, at adoption, run `sh scripts/leak-gate.sh history` with the real list declared. Confirm that every finding is an expected pre-adoption committed-rule finding and that none matches a list line.
  4. Only then delete the old file.
- R7. Every difference from the engineering-standards gate is recorded in the repository with its reason.
- R8. `make dev` installs the pinned gitleaks, installs the pre-commit and pre-push hooks, and runs `pre-commit run leak-gate --verbose` so the coverage line is visible (lead refinement 1).
- R9. The living docs that name the old gate describe the new one: `CONTRIBUTING.md`, `docs/dev/PUBLIC-SEED.md`, `docs/dev/RELEASING.md`, and the code comments that point readers at `publish_gate.py`.

### Key Decisions

- **Clean the tree rather than widen any allowlist, with one exception.** Upstream's allowlist is held shut by keeping it to well-known names, and the issue drops any buzai rule that needs an allowlist. A fixture assembled at run time is the convention upstream's own tests use. The one exception, decided by the operator after the rebase onto #27, is the `buzai` service account: a global allowlist entry for its exact name in buzai's block. It is global only because gitleaks 8.24.2 ignores the rule-scoped form. Literal paths to that account's home then stay in the docs, #27's plan and its tests. Governs R4, R5.
- **The SSN rule stays.** Its only hits are a well-known fake SSN in a redaction test in `trust/tests/test_units.py`, which becomes a value assembled at run time. That changes the tree, not the rule, so the issue's "dropped, not tuned" test is met. Signed off by the operator. Governs R4.
- **Path rewrites match their context** (operator condition). The service account's home is written as `~buzai/` only where a shell expands it, as `%h` in systemd lines, and as the literal path wherever a tool or a reader needs it. With the service-account allowlist in place, a literal path is never forced into a rewrite; placeholder homes in tests move off the `/home` root. Governs R4, R5.
- **The release check has a defined base** (operator condition). In `RELEASING.md`, when no release tag comes after this change, the range starts at the commit that merges it. The one-time full check is R6 step 3. Governs R7, R9.
- **The multi-ref push limit is closed by #12**, which pushes release tags by name (lead refinement 4). This plan only records it as an exception. Governs R7.

### Scope Boundaries

Out of scope, per #19: PR, issue and comment text (engineering-standards#74); hub content, which `scripts/hub_commit.py` scans separately; installed deployments (#18); `CODEOWNERS` on the gate files; deliberate bypasses. Dated plans, brainstorms and decision records that name `publish_gate.py` are history and stay as written. `CONTRIBUTING.md`'s maintainer range command is #21.

## Planning Contract

### Key Technical Decisions

- KTD1. **Seven files are copied byte for byte from `3ad35c7`:** `scripts/leak-gate.sh`, `scripts/gitleaks-report.tmpl`, `scripts/install-gitleaks.sh`, `scripts/test-leak-gate.sh`, `scripts/test-install-gitleaks.sh`, `.github/workflows/leaks.yml` and `.gitleaks.toml`. `leaks.yml` comes from `3ad35c7`, not main, because main's copy runs engineering-standards' own hook tests (engineering-standards#109). The copied comments still point at `process/repository-standards.md` and engineering-standards issue numbers. They stay as they are so that a later re-copy diffs cleanly.
- KTD2. **`.gitleaks.toml` is the upstream file plus one appended block,** so `head -n <upstream length>` still matches upstream exactly. The block holds:
  - `session-handoff-reference`, the one generic pattern from `publish_gate.py`;
  - `us-ssn-formatted`: `NNN-NN-NNNN`, excluding area numbers 000, 666 and 9xx, group 00 and serial 0000;
  - `card-number-grouped`: four groups of four digits, separated by spaces or by hyphens, plus the American Express 4-6-5 grouping.

  Each rule has a delimiter guard and `secretGroup = 1`, so it does not match inside longer digit runs, and the redacted finding is the number only. RE2 has no backreferences, so each separator gets its own alternative. Scanned over current `main`, the card rule has zero hits, the SSN rule has three (all one test fixture), and the handoff rule hits only `test_publish_gate.py`, which this change deletes.
- KTD3. **The shape-rule fixtures go in a buzai-owned `scripts/test-shape-rules.sh`, run from `ci.yml`, not `leaks.yml`.** This keeps `leaks.yml` verbatim. The script follows `test-leak-gate.sh`'s conventions:
  - planted values are built from shell variables, so the script matches none of its own rules;
  - a planted leak must exit exactly 3;
  - the planted text must be absent from the output;
  - every rule also has a near-miss that must pass (wrong grouping, mixed separators, longer digit run, excluded SSN area).
- KTD4. **`ci.yml` installs gitleaks with `scripts/install-gitleaks.sh` before `pre-commit run --all-files`.** The `leak-gate` hook refuses to run without gitleaks. The version and hashes stay in the install script only.
- KTD5. **`.pre-commit-config.yaml` drops the `gitleaks` repo hook and the `publish-gate` local hook,** and adds the two upstream framework entries (`leak-gate`, `leak-gate-pre-push`) verbatim. One gitleaks, pinned with its SHA-256, then runs locally and in CI.
- KTD6. **Tree cleanup is mechanical and changes no behaviour:**
  - the service user's home is rewritten by context, per the path-rewrite Key Decision;
  - tests use a non-`/home` root such as `/srv/<name>` where any absolute path works;
  - fake credentials and the SSN are concatenated at run time.

  The values the code under test receives do not change.
- KTD7. **The exceptions record is a new section of `docs/dev/PUBLIC-SEED.md`,** the living policy doc the gate already points to. It does not get a new file.

### Assumptions

- `/srv/<name>` paths satisfy the tests that currently use `/home/<name>`; `trust/tests/test_gate.py:223` is checked at build time, because it may depend on the path's shape.
- #17 (docs path references) and #24 (`RELEASING.md`) merge first. This branch rebases onto them and re-runs the tree scan after each rebase.
- pre-commit 3.2 or later, needed for the `stages` names; the proofs record the version used.

### Sequencing

U1 and U3 first (operator order). U2 and U4's doc edits come after #17 merges, rebased onto it, because #17 rewrites the same path lines in `docs/SETUP.md` and `docs/HOST-BOOTSTRAP.md`. `leaks.yml` stays red until U2 lands, so the pull request opens as a draft until then. It merges after #17.

## Implementation Units

### U1. Copy the gate and add buzai's shape rules

- Goal: the gate files exist in buzai, byte for byte except the appended block.
- Requirements: R4, R5, R7.
- Files: the seven KTD1 paths; `scripts/test-shape-rules.sh` (new).
- Approach: `git show 3ad35c7:<path>` for each file; append the KTD2 block; write the fixture script per KTD3.
- Test scenarios:
  - each shape rule refuses its planted value with exit 3, and the value is absent from the output;
  - each near-miss passes;
  - `test-shape-rules.sh` run against upstream's unmodified `.gitleaks.toml` fails on every planted value (seen to fail);
  - `test-leak-gate.sh` and `test-install-gitleaks.sh` pass with buzai's `.gitleaks.toml`.
- Verification: `cmp` of each copied file against `git show 3ad35c7:<path>`; `.gitleaks.toml`'s head against upstream.

### U2. Clean the tree

- Goal: `gitleaks dir .` with buzai's config reports zero findings on the tracked tree.
- Requirements: R4, R5.
- Files:
  - this plan, already written shape-only;
  - `docs/HOST-BOOTSTRAP.md`, `docs/SETUP.md`;
  - `scripts/tests/test_hub_paths.py`, `test_hub_init.py`, `test_secrets_preflight.py`, `test_hub_commit.py`, `test_hub_remote.py`;
  - `trust/tests/test_gate.py`, `trust/tests/test_units.py`.
- Approach: KTD6, one line at a time. No test changes what it asserts.
- Test scenarios: `make test` passes unchanged in count. The tree scan goes from 41 findings on `main` (35 upstream, 6 from buzai's candidate rules) to 0.
- Verification: the scan's before and after output, by file, line and rule only. Run it on a `git archive HEAD` export, never on the worktree, whose untracked `.venv` and `.git` file hold absolute paths.
- Pre-adoption commits keep their findings: history is not rewritten. Every check that scans history therefore uses a range that starts at or after this change (U4).

### U3. Wire it: hooks, CI, `make dev`, retire the old gate

- Goal: the gate runs at commit, at push and in CI, and nothing reads the old pattern file.
- Requirements: R1, R2, R3, R5, R6, R8.
- Files:
  - `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `Makefile` (`dev` target);
  - delete `scripts/publish_gate.py` and `scripts/tests/test_publish_gate.py`.
- Approach: KTD4 and KTD5. `make dev` runs the three steps in R8 in order and stops on the first failure. The Makefile's `PATH` export makes the pinned gitleaks the one `make dev` uses. Hooks fired by git use the committer's own `PATH`, or `GITLEAKS`. The `CONTRIBUTING.md` line says so in one clause (U4).
- Test setup: a scratch clone with a scratch `HOME`. A canary list is declared with `git config --local leakgate.values`, pointing at a file outside the clone. Seed a scratch bare remote with `main` and fetch it before installing the hooks. Otherwise the first push scans all of pre-adoption history, and every proof fails on unrelated findings.
- Test scenarios:
  - stage a canary → commit refused with `list line N`, canary absent from output; clean commit succeeds (R1);
  - canary in a commit message, push → refused with only the value-rule reason and no committed-rule findings; the remote ref is unchanged (R2);
  - no declaration → commit and push succeed; `pre-commit run leak-gate --verbose` prints the skip line (R3);
  - `make dev` in the scratch clone prints the coverage line (R8);
  - `git grep gate-patterns` finds only dated history docs (R6).
- Verification: commands and output go in the pull request body.

### U4. Docs and exceptions

- Goal: the living docs describe the new gate, and the exceptions are recorded.
- Requirements: R7, R9.
- Files:
  - `docs/dev/PUBLIC-SEED.md`, `docs/dev/RELEASING.md`, `CONTRIBUTING.md` (the `make dev` line);
  - `scripts/hub_init.py:182` and `scripts/hub_commit.py:38`, worded so they no longer point at a live file;
  - `scripts/tests/test_hub_commit.py:417`.
- Approach:
  - PUBLIC-SEED: enforcement by the gate; the list declared with `git config leakgate.values`; the new "Differences from the engineering-standards gate" section.
  - RELEASING: the release-time run becomes `sh scripts/leak-gate.sh range <previous release tag>..<release commit>` with the list declared. When no release tag comes after this change, the range starts at the commit that merges it. It is not `history`, because pre-adoption history holds committed-rule findings and would fail every release. Rebased on #24.
  - CONTRIBUTING: the `make dev` line says that hooks find gitleaks on the user's own `PATH` (`~/.local/bin` first) or through `GITLEAKS`.
  - `hub_init.py`, `hub_commit.py` and the test comment refer to the former publish gate in the past tense, without a path to a live file.
  - `scripts/secrets_preflight.py`'s docstring, which #19 names, never mentions the old gate and stays as it is.
  - New prose is written shape-only, like this plan: no literal home path, handoff filename, SSN or card number. The new committed rules have no exemption for the docs that describe them.
- Exceptions to record:
  - the appended rules;
  - fixtures in `test-shape-rules.sh`, run from `ci.yml`;
  - `leaks.yml` from `3ad35c7`;
  - the gitleaks install in `ci.yml`;
  - the multi-ref push limit, closed by #12;
  - no `CODEOWNERS`;
  - shape rules apply to file contents only. Commit and tag messages, paths, ref names and identities are checked against the value list alone, because `leak-gate.sh` is copied verbatim;
  - the release check is ranged rather than `history`.
- Verification: `git grep -n publish_gate` outside `docs/plans`, `docs/brainstorms` and `docs/decisions` returns nothing.

### U5. Review and pull request

- Goal: the change is reviewed and green.
- Requirements: all.
- Files: none beyond the above.
- Approach: code review of the diff before opening the pull request. Record each finding's disposition, and re-review any fix commits. Open the pull request; its Related line closes the issue, because this pull request completes it. Read CI on the head commit until it is green.
- Verification: `gh pr view --json closingIssuesReferences` shows only 19.

## Verification Contract

- `make test` (stdlib unittest, `.venv` Python) and `pre-commit run --all-files`, in this worktree without installing hooks.
- This machine declares a value list in git config. Every leak-gate run in this worktree therefore switches the list off at command scope, which overrides global and local config: `GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=leakgate.values GIT_CONFIG_VALUE_0=none`. A run is trusted only if its output shows the line saying value rules were skipped. The upstream test scripts already isolate themselves with an empty global config.
- `sh scripts/test-leak-gate.sh`, `sh scripts/test-install-gitleaks.sh` and `sh scripts/test-shape-rules.sh`, with the pinned gitleaks from `scripts/install-gitleaks.sh` installed to a scratch directory.
- `gitleaks dir` with the `leaks.yml` flags over a `git archive HEAD` export: zero findings.
- The U3 hook proofs, run only in scratch clones with a scratch `HOME`. Never run `make dev` or `pre-commit install` in the real clone or its worktrees.
- Tool versions (gitleaks, pre-commit, git, Python) are named in the pull request body. CI on the pushed head is the conclusion.

## Definition of Done

- R1 to R9 hold, each with evidence in the pull request body. Each refusal shows its reason text, not just an exit code.
- Every new check was seen to fail before it was trusted.
- The copied files match `3ad35c7` byte for byte, apart from the appended `.gitleaks.toml` block.
- No real value list was read; fixtures use invented canaries only.
- No dead code from abandoned approaches remains in the diff.
- The pull request lists the operator's post-merge steps from R6, in order.
