---
title: No GitHub account credential on a deployment - Plan
type: fix
date: 2026-10-10
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
origin: https://github.com/rmorison/buzai/issues/29
---

# No GitHub account credential on a deployment - Plan

## Goal Capsule

- Objective: an installed deployment that holds a way to publish to GitHub, other than the repo-scoped hub deploy key, refuses to start and says which kind of credential it found, without printing it. An operator reading the docs knows the service account must hold no GitHub account credential, and a clean fresh install starts exactly as it does today.
- Means: new fatal checks beside `origin_credential_violations` in `scripts/secrets_preflight.py`, one new warning, and a policy line in the deploy docs (KTD1-KTD8).
- Authority: issue #29, then the #18 spike findings it cites, then this plan. Where this plan departs from the issue it says so under Spec drift, and the operator's sign-off on #29 settles each item.
- Stop conditions: stop and comment on #29 if a clean fresh install raises a new finding, if a check can only be proven by reading a real credential, or if the work needs a change to `scripts/hub_paths.py` (#22 owns it).
- Execution profile: one pull request on `29-no-github-credential`, after #22 merges (rebase, then the full `make test` again). The worker builds after the operator signs off on #29. The operator merges.

## Product Contract

### Summary

The preflight already refuses to start when a credential helper resolves for the public origin. This adds the other routes the #18 spike found: a credential in the origin's fetch or push URL, a push redirected by `pushurl` or a URL rewrite, a `~/.netrc` entry for the origin's host, a `gh` login, and a `GH_TOKEN`/`GITHUB_TOKEN` in the service environment. Each is fatal and reports the kind of credential, never its value. Untracked, non-ignored files in the checkout raise a warning listing their names. The deploy docs gain the policy line that closes the routes no check can see.

### Problem Frame

`origin_credential_violations` asks one question: does a credential helper resolve for the origin URL. A deployment can still push to the public repo, or post to GitHub, by routes that need no helper. Each route needs a credential someone added to the box by hand, so the risk is a mistake rather than an attack, and a start-time refusal is the right control: it is the one moment the operator reads the journal. The assistant's working directory is the public clone (`~/buzai-assistant`, after #17), so its scratch files land there untracked and not ignored, one `git add -A` from a commit.

### Requirements

- R1. `deploy/README.md` and `docs/SETUP.md` state the policy: the service account holds no GitHub account credential. That means no account-wide ssh key (`~/.ssh/id_*` or an agent), no personal access token, no `~/.netrc` entry and no `gh` login. Its only GitHub credential is the repo-scoped hub deploy key. The line names `gh auth login` with git setup declined, `gh auth login --with-token`, and `GH_TOKEN`/`GITHUB_TOKEN` explicitly, and says a managed claude.ai GitHub connector can't be seen from disk.
- R2. Fatal: any remote's `url` or `pushurl` in the checkout, or any effective push URL of origin, embeds a credential (`url_credential_problem` is true of it). The finding names the remote and key, never the URL's userinfo.
- R3. Fatal: an effective push URL of origin is a network URL (a non-`file` transport scheme, or scp-like `host:path`) that differs from `remote.origin.url`, meaning a `remote.origin.pushurl` or a `url.<base>.pushInsteadOf`/`insteadOf` rewrite redirects pushes. A push URL that names no network transport, such as the `no_push` hardening idiom, can't publish and is not a finding. The finding names the mechanism by key shape only.
- R4. Fatal: `~/.netrc` has a `machine` entry for the origin's host, `github.com` or any host under `.github.com` (such as `api.github.com`), or a `default` entry. Only `machine` names are parsed; no other field is read into a message.
- R5. Fatal: `gh`'s `hosts.yml` has a top-level entry for the origin's host, `github.com` or any host under `.github.com`.
- R6. Fatal: `GH_TOKEN`, `GITHUB_TOKEN` or `GITHUB_PERSONAL_ACCESS_TOKEN` (the variable the GitHub MCP server reads) is set to a non-empty value in the environment the preflight runs in. Presence only.
- R6a. Fatal: `core.askPass` resolves in the checkout's git config, or `GIT_ASKPASS` is non-empty in the environment. Reported by key or variable name only. A headless service account has no use for either, and a program there can hand git a push credential with no helper configured. `SSH_ASKPASS` is not checked: some distributions set it system-wide for desktop sessions.
- R7. A check that cannot answer (git failed or timed out, a file exists but cannot be read) is a fatal "unverified rather than clean" finding, as the module's other leak checks are.
- R8. Warning, never fatal: untracked, non-ignored files in the checkout, listed by name (first five, then a count), with the remedy.
- R9. No message, test name, fixture output or log line carries a credential value. Fixtures use invented canary values assembled at run time.
- R10. Each new check has a test that plants its violation, is seen to fail first, and drives the real composition through `main()`.
- R11. A clean fresh install passes the preflight with no new finding.
- R12. Optional (issue item 4): the commented GitHub preset in `trust/config/connector-legs.toml` binds `mcp__github__create_*`, `add_comment` and `update_*` to the always-gate `publish` class in its hard-gate note.

### Spec drift (each needs the operator's sign-off on #29)

- D1. R3 covers plain `insteadOf` as well as `pushInsteadOf`, and judges any rewrite by its effect on origin's push URL rather than by its presence. A plain `insteadOf` rewrites pushes too (verified with git 2.34.1 in a scratch repo), and a rewrite that never touches origin can't push the checkout. Recommended.
- D2. `scripts/hub_init.py` `owner_instructions` and `docs/SETUP.md` §9b tell the owner to run `gh repo create hubs --private`. With R5 that command, run as the service account, would leave a `gh` login there and stop the service. Both say to run it from the owner's own machine or the web UI. Recommended: without it, the docs lead the owner into the failure.
- D3. R12 is included: a comment-only change to an opt-in preset. Recommended.
- D4. R6a adds a `core.askPass`/`GIT_ASKPASS` check. The issue lists askPass as a route but not as a check; the plan review found it is a helper-equivalent route with no legitimate use on the service account. Account-wide ssh keys stay with the policy line, out of scope by the issue. Recommended.
- D5. R6 adds `GITHUB_PERSONAL_ACCESS_TOKEN`, and R4/R5 match hosts under `.github.com`. A token for `api.github.com` in `.netrc` posts without `gh`, and the GitHub MCP server reads its own variable. Found by the plan review. Recommended.
- D6. R2 covers every remote in the checkout, not only origin: a second remote with an embedded token pushes the checkout just as well. Found by the plan review. Recommended.

### Scope Boundaries

- Out, per the issue: account-wide ssh keys as a preflight failure; managed claude.ai connectors; the account-level checks on the operator's own fresh install (they stay on #18).
- Out: `scripts/hub_paths.py` (#22), `CLAUDE.md` (#30), `CONTRIBUTING.md` (#21).
- Out: a behavioural `git push --dry-run` probe. It would make an authenticated network call on every start.
- Deferred, proposed as a follow-up for the lead to file: presence of `GH_TOKEN`/`GITHUB_TOKEN`/`GITHUB_PERSONAL_ACCESS_TOKEN` by key name in Claude Code settings `env` blocks and in `~/.claude.json` MCP server env blocks. It needs JSON parsing of files this module doesn't read today, and the issue scopes the env check to the service environment. The policy line covers it meanwhile.

## Planning Contract

### Key Technical Decisions

- KTD1. The new checks live in `scripts/secrets_preflight.py`, not `scripts/hub_remote.py`. `.netrc`, `gh` and the environment are about the deployment's account, not the hub remote. They reuse `hub_remote`'s `url_credential_problem` (as a predicate only, since its message names the hub remote), `config_url`, `redact`, `LOCAL_ENV` and the injected `GitRunner`, so nothing is reimplemented. One gatherer, `account_credential_violations(repo_root, runner, *, home, environ, timeout)`, returns the list; small pure deciders do the judging (`push_route_problems`, `netrc_hosts`, `gh_hosts`, `token_env_problems`) so each is testable without git.
- KTD2. R3 compares `git remote get-url --push --all origin` with `git config --get remote.origin.url`, and counts a difference only when the push URL is a network URL (`hub_remote.TRANSPORT_SCHEMES` minus `file`, or `SCP_LIKE`). git applies `pushurl`, `pushInsteadOf` and `insteadOf` itself, so the comparison covers all three without reimplementing git's longest-prefix rule. The mechanism is then named from `git config --name-only --get-regexp` over `remote.origin.pushurl` and `url.*.(push)insteadof`. Key names are printed by shape only (`remote.origin.pushurl`, `a url.<base>.pushInsteadOf rewrite`), because a rewrite's key embeds its base URL and that base can hold a token.
- KTD3. Hosts checked by R4 and R5 are the origin's host (from `config_url` then `urlsplit`), `github.com`, and any host ending in `.github.com`, case-insensitive. R2 reads every remote's `url` and `pushurl` with one `git config --get-regexp '^remote\..*\.(url|pushurl)$'`; values are judged in memory and never printed. `github.com` is always in the set so a missing or local origin can't silence the check. With no origin configured, R2 and R3 have nothing to judge and add no finding; the existing helper check already reports "this checkout".
- KTD4. `.netrc` is tokenized by hand, not with the stdlib `netrc` module. The module reads every password into memory and its parse errors can quote the file; the hand tokenizer keeps only the token after `machine` and notes `default`, and skips `macdef` bodies up to the blank line. The path is `<home>/.netrc`.
- KTD5. `gh`'s hosts file is `$GH_CONFIG_DIR/hosts.yml`, else `$XDG_CONFIG_HOME/gh/hosts.yml`, else `<home>/.config/gh/hosts.yml`, read from the injected `environ`. Its top-level keys are read line by line (column 0, not a comment, quotes stripped, text before the first `:`), so no YAML dependency is added and no value is read. A newer `gh` keeps the token in the system keyring, but still writes the host entry, so the entry is the signal.
- KTD6. The untracked-files warning runs `git ls-files --others --exclude-standard --directory --no-empty-directory -z` in the checkout through the runner. It is a warning gatherer, so it never raises: a git failure becomes a warning that the check couldn't run, and the call sits inside the same catch-all discipline as `durability_report`.
- KTD7. `fatal_problems` gains an `account_violations` keyword, placed after `origin_violations`, so dropping it from `assess()` fails a composition test. Messages follow the existing style: what was found, why it is fatal, and the remedy command with no value in it (for example `git -C <checkout> config --unset remote.origin.pushurl`, `gh auth logout`, remove the variable from the unit's `EnvironmentFile`).
- KTD8. R6 reads the preflight's own environment. Under systemd that is the service environment, since `ExecStartPre` gets the unit's `Environment=` and `EnvironmentFile=`. Under `make doctor` or `make smoke` it is the operator's shell, which is the account being checked too. The docs say so. Tokens placed in Claude Code settings `env` blocks or in a local MCP server's env in `~/.claude.json` reach the assistant without entering this environment; they are not checked here (see Scope Boundaries).

### Sequencing

U1 → U2 → U3 → U4, U5 independent. Rebase onto origin/main after #22 merges and rerun the whole suite (`secrets_preflight.py` imports `hub_paths`).

## Implementation Units

### U1. Origin push-route checks

- Goal: R2, R3, R7 for git config.
- Files: `scripts/secrets_preflight.py`, `scripts/tests/test_secrets_preflight.py`.
- Approach: KTD1, KTD2, KTD3, R6a. Every remote's URLs from `config --get-regexp`; raw origin from `config --get remote.origin.url`; askPass from `config --get core.askPass`; effective push URLs from `remote get-url --push --all origin`; mechanism names from `config --name-only --get-regexp`. Exit 1 from `config --get` means no origin (no finding); any other nonzero, or a timeout, is an unverified finding.
- Test scenarios (real scratch repos under a tempdir, hermetic git config as the file already sets):
  - clean https origin: no finding;
  - origin URL with an invented `user:token@` (assembled at run time): one finding that names the shape and does not contain the token;
  - `pushurl` set to a different URL: one finding naming `remote.origin.pushurl`;
  - `pushurl` with an embedded token: findings for both, none containing the token;
  - `pushInsteadOf` rewriting origin: one finding naming a pushInsteadOf rewrite, and not containing the rewrite base;
  - plain `insteadOf` rewriting origin: one finding;
  - a `pushInsteadOf` for an unrelated host: no finding;
  - `pushurl` equal to `url`: no finding;
  - `pushurl` set to `no_push`: no finding;
  - a second remote `fork` whose URL embeds an invented token: one finding naming `remote.fork.url`, token absent;
  - `core.askPass` set: one finding naming `core.askPass`, its value absent;
  - no origin: no finding;
  - a runner that times out or exits 128: one unverified finding.
- Verification: each plant test fails before the check is wired (seen-to-fail output recorded for the pull request).

### U2. Account credential files and environment

- Goal: R4, R5, R6, R7, R9.
- Files: `scripts/secrets_preflight.py`, `scripts/tests/test_secrets_preflight.py`.
- Approach: KTD3, KTD4, KTD5, KTD8. The environment half of R6a lives here with R6.
- Test scenarios:
  - no `.netrc`: no finding; `.netrc` with `machine github.com login x password <canary>`: one finding, canary absent from every message; `machine` for the origin's host on its own line layout (tokens split across lines); `default` entry: finding; `machine example.org` only: no finding; host in mixed case: finding; a `macdef` body containing the word `machine github.com`: no finding; `.netrc` present but unreadable (mode 000, skipped when running as root): unverified finding;
  - no hosts file: no finding; `hosts.yml` with `github.com:` and a nested `oauth_token: <canary>`: one finding, canary absent; quoted key `"github.com":`: finding; only a nested key named `github.com`: no finding; `GH_CONFIG_DIR` and `XDG_CONFIG_HOME` each move the file the check reads; unreadable file: unverified finding;
  - `machine api.github.com login x password <canary>`: one finding, canary absent; `machine notgithub.com`: no finding;
  - `GH_TOKEN` set to a canary: one finding naming `GH_TOKEN`, canary absent; `GITHUB_TOKEN` and `GITHUB_PERSONAL_ACCESS_TOKEN` likewise; two set: two findings; set to empty: no finding;
  - `GIT_ASKPASS` set: one finding naming it, value absent; `SSH_ASKPASS` set: no finding.
- Verification: as U1.

### U3. Composition and the untracked-files warning

- Goal: R7, R8, R10, R11; KTD6, KTD7.
- Files: `scripts/secrets_preflight.py` (module docstring's FATAL and WARNING lists, `fatal_problems`, `assess`), `scripts/tests/test_secrets_preflight.py`.
- Approach: `assess` calls `account_credential_violations(repo_root, runner, home=home, environ=environ)` and passes it to `fatal_problems`; the untracked warning is appended next to the credentials warning.
- Test scenarios (subclass `AssessCompositionCase`):
  - each U1 and U2 plant through `assert_only_fatal`, so start is blocked with exit 1 and the FAIL line printed;
  - the existing "every planted leak at once" test gains the new plants;
  - an untracked scratch file in the fixture checkout: one warning naming it, exit 0; an ignored file (under a planted `.gitignore` rule): no warning; seven untracked files: five names and a count; a runner failure on `ls-files`: a warning, exit 0;
  - the clean-instance test stays green with no change to its fixture (R11 at the unit level);
  - a canary scan: run `main()` with every plant at once and assert no canary appears in stdout or stderr.
- Verification: `make test`; then a fresh clone of the branch into a scratch directory, run with a scratch `HOME` and empty environment, prints no new FAIL or WARN line (R11 end to end). Commands and output go in the pull request body.

### U4. Policy line and docs

- Goal: R1, D2.
- Files: `deploy/README.md` (a short "No GitHub account credential" subsection beside "The hub remote credential", and the `FAIL:`/`WARN:` lists in the preflight section), `docs/SETUP.md` (the same policy line where §9b introduces the deploy key, and §9b's `gh repo create` step), `scripts/hub_init.py` (`owner_instructions` wording only), `docs/TROUBLESHOOTING.md` (one entry: what each new FAIL means and its fix, beside the existing credential-helper entry), the existing "deploy key or token" and "fine-grained tokens expire" wording in `docs/SETUP.md`, `docs/TROUBLESHOOTING.md` and `deploy/README.md` (the hub credential is the deploy key only, so the docs stop pointing operators at a token that R4/R5 would now refuse), the matching phrase in `backlog_warning`, `scripts/tests/test_hub_init.py` if it asserts the instruction text.
- Approach: plain language, by role. Remedies never include a value. Public-safe per `CLAUDE.md`: no hostnames, account names or instance paths beyond the documented defaults.
- Test scenarios: existing doc-layout and hub-init tests stay green; if `test_hub_init.py` pins the text, update it to the new wording.
- Verification: `make test`, `make lint`, `pre-commit run --all-files`.

### U5. GitHub preset hard-gate line (optional, D3)

- Goal: R12.
- Files: `trust/config/connector-legs.toml` (comment block only).
- Approach: extend the commented GitHub block's hard-gate note with `"mcp__github__create_*" = "publish"`, `"mcp__github__add_comment*" = "publish"`, `"mcp__github__update_*" = "publish"`, matching the Atlassian block's shape.
- Test scenarios: none new; the trust suite stays green (a comment can't change classification, which the suite proves by passing unchanged).
- Verification: `make test`.

## Verification Contract

- `make test` (stdlib unittest, `.venv/bin/python` 3.12), `make lint`, `pre-commit run --all-files`; tool versions named in the pull request body.
- Every new check seen to fail before it is trusted: for each plant, run its test against the module with that check not yet wired, record the failing assertion, then wire it.
- Refusals asserted by their reason text, not just the exit code.
- The fresh-clone run from U3.
- After pushing: CI conclusions `checks` and `leaks` on the head read until success.
- After #22 merges: rebase onto origin/main and rerun the full `make test`.

## Definition of Done

- R1-R11 (with R6a) met, and R12 if the operator keeps D3; each spec-drift item signed off or removed.
- A document review of this plan and a code review of the diff ran, with each finding's disposition in the pull request body, and fix commits reviewed again.
- No credential value in any message, test output, commit or GitHub text.
- No dead-end code from abandoned approaches left in the diff.

## Sources

- Issue #29; the #18 spike findings (issue comment 6071110498).
- `scripts/secrets_preflight.py` module docstring: the fatal/warning split, "not a no-op", bounded git calls.
- `scripts/hub_remote.py`: `origin_credential_violations`, `url_credential_problem`, `helper_shape`, `config_url`, `redact`.
- `docs/decisions/private-versioned-hubs.md` on why `gh` is not used on the box.
- Scratch-repo check of `git remote get-url --push --all` under `pushurl`, `pushInsteadOf` and `insteadOf` (git 2.34.1).
