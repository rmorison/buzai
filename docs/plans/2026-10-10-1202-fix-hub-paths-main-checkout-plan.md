---
title: Refuse hub paths inside the main checkout from a worktree - Plan
type: fix
date: 2026-10-10
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
origin: https://github.com/rmorison/buzai/issues/22
---

# Refuse hub paths inside the main checkout from a worktree - Plan

## Goal Capsule

- Objective: when buzai's hub tools run from a linked git worktree, they refuse a hub location inside the main checkout, just as they already refuse one inside the checkout they run from, so personal hub content can't be written into the public repository. Runs from a normal clone behave exactly as before.
- Means: the resolver finds the main checkout that a linked worktree belongs to and refuses that tree too (KTD1, KTD2).
- Authority: issue #22, then this plan. Where they disagree, the issue wins.
- Stop conditions: stop and comment on #22 if the fix needs `scripts/secrets_preflight.py` changed (issue #29 owns it), if any importer's tests change behaviour from a normal clone, or if a proof can only pass by touching the real checkout or real hub.
- Execution profile: one pull request on `22-hub-paths-worktree`, merged before #29. The worker builds after the operator signs off on #22. The operator merges.

---

## Product Contract

### Summary

`scripts/hub_paths.py` refuses a hub path inside "the checkout", but it takes the checkout to be the directory its own file sits in. Run from a linked worktree, that is the worktree, so a hub path elsewhere in the main checkout passes. The fix makes the resolver refuse the main checkout as well.

### Problem Frame

Agent workers run from worktrees nested in the main checkout (`.claude/worktrees/<name>/`). From there, `REPO_ROOT` is the worktree. Setting `BUZAI_HUBS_DIR` to `<main checkout>/hubs/x` passes both containment checks: it is not inside the worktree, and it does not contain the worktree. `hub_commit.py` would then write hub content into the public main checkout, where only `.gitignore` and the preflight stand between it and a public remote. CLAUDE.md promises the resolver refuses any path inside the checkout; from a worktree, that promise doesn't hold. Found by the code review of the #20 pull request; it predates #20.

### Requirements

**Refusal**

- R1. Run from a linked worktree, the resolver refuses a hub path inside the main checkout, including the main checkout's root, with a reason that names the main checkout.
- R2. Run from a linked worktree, the resolver refuses a hub path that contains the main checkout.
- R3. The existing refusals, inside or containing the checkout the code runs from, are unchanged, and both comparisons still run after symlinks are resolved.
- R4. If the resolver runs from a linked worktree but can't find the main checkout, it refuses rather than carrying on unchecked.

**Compatibility**

- R5. From a normal clone, and from a directory that isn't a git checkout at all, behaviour is unchanged and no git command runs.
- R6. Every existing caller keeps working unedited: `refusal(resolved, repo_root)`, `resolve(raw, home, repo_root)`, `expand` and `source_of` keep their signatures and meaning. `scripts/secrets_preflight.py` is not edited.
- R7. The full `make test` passes, not only `scripts/tests/test_hub_paths.py`: `hub_commit`, `hub_review`, `hub_init`, `hub_remote` and `secrets_preflight` all import this module.

### Acceptance Examples

- AE1. Covers R1. Given a scratch main repository with a linked worktree at `<main>/.claude/worktrees/w`, and `BUZAI_HUBS_DIR=<main>/hubs/x`, running the worktree's `scripts/hub_paths.py` exits 1 with `hub-paths FAIL` and a reason naming `<main>` as the main checkout. Before the fix, the same run exits 0.
- AE2. Covers R5. The same run from `<main>` itself, with a hub path outside it, exits 0 and prints the resolved path.

### Scope Boundaries

- Sibling worktrees (other linked worktrees of the same repository) are not refused. The rule is "the checkout this code runs from and the main checkout it belongs to"; a hub in some other copy of buzai elsewhere on disk isn't caught either, and listing every worktree would make the refusal depend on transient state.
- The preflight keeps calling `refusal(path, repo_root)` with one root, so its hub-path check stays blind to the main checkout until it adopts the new helper. #29 owns that file; the follow-up is named under Deferred.
- No change to CLAUDE.md (#30 owns it). Its rule already says the resolver refuses any path inside the checkout; this fix makes that true.

### Deferred

- `scripts/secrets_preflight.py` passing the main checkout to `refusal` (one line, using `main_checkout`). Raised with the lead for #29, which rebases after this merges.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **Find the main checkout with `git worktree list --porcelain`, run only when `<repo_root>/.git` is a file.** A directory `.git` is a normal clone and needs no lookup, so R5 holds without a subprocess. A file `.git` means a linked worktree (or a submodule); the first `worktree` record of the porcelain listing is the main working tree, or carries a `bare` line when there is none. Git's own answer covers layouts that hand-parsing `.git`/`commondir` gets wrong (`--separate-git-dir`, `core.worktree`). Git 2.34 on the host supports `--porcelain`; `-z` (2.36) is not used. Chosen over parsing the gitdir and `commondir` files: fewer edge cases, at the cost of one subprocess in worktrees only.
- KTD2. **A new `main_checkout(repo_root) -> Path | None` does the lookup; `refusal` gains an optional `main_root`; `resolve` calls `main_checkout` itself.** `refusal` stays pure and backward compatible (R6). Putting the lookup in `resolve` means every caller of `resolve`, and `hub_dir()`, gets the protection without passing anything new. `None` means "no other checkout to protect": a normal clone, no `.git`, a bare main repository, or a main checkout equal to `repo_root`.
- KTD3. **Fail closed.** When `.git` is a file and git is missing, times out, exits non-zero or prints no `worktree` record, `main_checkout` raises `HubPathError` naming the worktree and the cause (R4). The service runs from a normal clone, so this costs only a worktree session.
- KTD4. **The lookup runs with a scrubbed environment.** `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR` and the other `GIT_*` variables are removed for the child, so an inherited variable can't point the lookup at another repository. A short timeout bounds it. `hub_remote`'s env overlays can't be reused: `hub_remote` imports `hub_paths`.
- KTD5. **The main checkout's path is resolved before comparison**, like the other two paths, so a symlinked main checkout can't slip through a lexical comparison.

### High-Level Technical Design

Directional only:

```text
resolve(raw, home, repo_root):
    root = repo_root.resolve()
    main = main_checkout(root)            # None for a normal clone; raises if lookup fails in a worktree
    problem = refusal(expand(raw, home).resolve(), root, main)
    ...

refusal(resolved, repo_root, main_root=None):
    existing two checks against repo_root
    if main_root and main_root != repo_root:
        inside main_root      -> "... is inside the main checkout (<main>) that this worktree belongs to ..."
        contains main_root    -> "... contains the main checkout (<main>) ..."
```

### Assumptions

- Agent worktrees are linked worktrees made by `git worktree add`; their `.git` is a file.
- The deployed service runs from a normal clone, so production never runs the lookup.

---

## Implementation Units

### U1. Refuse the main checkout in `scripts/hub_paths.py`

- **Goal:** R1 to R6.
- **Requirements:** R1, R2, R3, R4, R5, R6.
- **Dependencies:** none.
- **Files:** `scripts/hub_paths.py`, `scripts/tests/test_hub_paths.py`.
- **Approach:** add `main_checkout` (KTD1, KTD3, KTD4), extend `refusal` with `main_root=None` (KTD2), call `main_checkout` from `resolve` and resolve its result (KTD5). Update the module docstring: a third refused shape, and why the lookup runs only in worktrees.
- **Patterns:** the existing split between pure `refusal` and filesystem-touching `resolve`; existing reason wording ("inside the repo checkout", naming `ENV_VAR`).
- **Test scenarios** (pure `refusal`, no filesystem):
  - a path inside `main_root` is refused, and the reason names the main checkout and `ENV_VAR`;
  - `main_root` itself is refused;
  - a path containing `main_root` is refused;
  - a sibling of `main_root` sharing a name prefix is fine;
  - `main_root=None`, and `main_root == repo_root`, give exactly today's results.
- **Test scenarios** (`main_checkout` and `resolve` against scratch repositories in a temp dir, git run with a scratch `HOME` and no global or system config):
  - a normal clone returns `None` and runs no git (asserted by making git unavailable on `PATH` for that case);
  - a directory with no `.git` returns `None`;
  - a linked worktree nested in the main checkout returns the resolved main checkout;
  - a linked worktree outside the main checkout returns the main checkout too;
  - a `.git` file whose gitdir is missing or broken raises `HubPathError` with the cause;
  - git unavailable while `.git` is a file raises `HubPathError`;
  - an inherited `GIT_DIR` pointing at another repository doesn't change the answer;
  - `resolve` from the worktree refuses `<main>/hubs/x`, and accepts a path outside both;
  - from the worktree, a hub path reached through a symlink into the main checkout is refused, and so is `<main>/hubs/x` when the main checkout is itself reached through a symlink (R3, KTD5).
- **Test scenario** (end to end, AE1 and AE2): copy the module under test into a scratch main repository, commit it, add a linked worktree at `.claude/worktrees/w`, and run `<worktree>/scripts/hub_paths.py` with `BUZAI_HUBS_DIR=<main>/hubs/x` and a scratch `HOME`. Assert exit 1, `hub-paths FAIL`, and the main checkout's path in the reason. Then run `<main>/scripts/hub_paths.py` with a hub path outside the main checkout, and assert exit 0 and the resolved path printed (AE2). The worktree run is seen to fail against the current module first (exit 0), with the output recorded in the pull request.
- **Verification:** `.venv/bin/python -m unittest scripts.tests.test_hub_paths` passes; the end-to-end test fails on `origin/main`'s module.

### U2. Run every importer's suite and the repo checks

- **Goal:** R7.
- **Requirements:** R5, R6, R7.
- **Dependencies:** U1.
- **Files:** none expected; any change found necessary goes back to U1 or stops under the Goal Capsule.
- **Approach:** run the full suite from this worktree. Since the worktree is itself a linked worktree, the existing `TestHubDir` and `TestMain` cases now also exercise the lookup against the real main checkout, read-only.
- **Verification:** see the Verification Contract.

---

## Verification Contract

| Check | Command | Done when |
|---|---|---|
| Unit tests | `make test` | all pass, both suites |
| Lint | `make lint` | clean |
| Hooks | `pre-commit run --all-files` | clean, without installing hooks |
| New test seen to fail | the end-to-end test run against `origin/main`'s `scripts/hub_paths.py` | fails with exit 0 where 1 is expected; output in the pull request |
| CI | `checks` and `leaks` on the pushed head | success |

All fixtures live in temp directories; nothing is written inside this checkout, and no test reads or writes the real hub.

---

## Definition of Done

- R1 to R6 hold, each covered by a named test in `scripts/tests/test_hub_paths.py`; R7 holds by the full `make test` run.
- The module docstring describes the third refused shape.
- The pull request records the before-fix failure, tool versions, and each review finding's disposition.
- The preflight follow-up is raised with the lead for #29.
- No dead code from abandoned approaches remains in the diff.
