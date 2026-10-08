# PUBLIC-SEED — publication policy for this repo

Living policy for what may appear in this public repo and what the leak gate
protects. Everything ships except PII/personal-infra details and the files
explicitly marked below. Enforced by the leak gate: `scripts/leak-gate.sh` at commit
and push, and `.github/workflows/leaks.yml` in CI. The gate is engineering-standards'
(`process/repository-standards.md` there, "Leak Gate"), copied whole; buzai's
differences are listed below.

## Scrub rule categories

These are *categories*, deliberately generic. The literal values (actual hostnames,
account handles, box identifiers) live in the maintainer's **private value list,
outside every repo**, declared per machine or per clone with
`git config leakgate.values <path>`. Never commit literal sensitive strings to this
file or anywhere in the tree.

1. **Personal hostnames / box identifiers** — any reference identifying the
   maintainer's real machines or domains (except intentionally public branding sites).
2. **Account/user handles tied to real infrastructure** — SSH users, service accounts
   on real boxes.
3. **Emails** — except public contact addresses deliberately published.
4. **Private working-notes artifacts** — session-handoff files and similar; reference
   them as "(session handoff notes, private archive)".
5. **Credentials of any shape** — never present; gitleaks' default rules in the gate
   enforce this regardless of the list.
6. **Personal financial data** — never present, not even as a test value; build test
   values at run time. Shape rules refuse a formatted social security number and a
   card number written in groups.

## How the gate runs

- **Committed rules** (`.gitleaks.toml`) are shapes, never values: gitleaks' default
  credential rules, an absolute home-directory path, and buzai's shape rules. They
  run on what a commit stages, on what a push sends, and in CI over every commit a
  change adds and over the whole tree.
- **Value rules** are generated from the private list and run only on a machine that
  declares it. They also check file paths and commit messages, so a push whose
  message holds a value is refused before it leaves the machine. A refusal names
  the list line, never the value.
- **Without a list** (an outside contributor, CI), the gate runs the committed rules
  and prints one line saying value rules were skipped.
- `make dev` installs the pinned gitleaks into `~/.local/bin` (the version and
  SHA-256 live only in `scripts/install-gitleaks.sh`), installs the pre-commit and
  pre-push hooks, and runs the gate once to show its coverage line. The hooks use
  the first `gitleaks` on your own `PATH`, or `GITLEAKS`.
- Before merging a pull request, a maintainer with the list runs the value rules
  over its commits from the default branch's checkout: the standard's pre-merge
  check in "Running It Locally".

## Differences from the engineering-standards gate

Every gate file is a byte-for-byte copy of rmorison/engineering-standards at
`3ad35c7`, except as listed here. Re-copying on an upgrade keeps everything after the
"buzai additions" marker in `.gitleaks.toml`.

| Difference | Reason |
|---|---|
| Shape rules appended to `.gitleaks.toml`: formatted social security numbers, card numbers in groups, and references to a session-handoff notes file | The installed project handles personal financial data, which no value list can enumerate. The handoff pattern was the one committed rule of buzai's earlier gate. |
| A global allowlist entry for the `buzai` service account's home directory | The account is documented and created by every install; it is not personal data. Literal paths keep the docs accurate, such as a command's expected output, and dated plans stay unedited. It is global only because gitleaks 8.24.2 ignores the rule-scoped `[[allowlists]]` form; on the next gitleaks upgrade, move it to `[[allowlists]]` with `targetRules = ["home-directory-path"]` if supported. It exempts the exact name only: any other user directory, a lookalike included, is refused. |
| `scripts/test-shape-rules.sh` proves the additions, and `ci.yml` runs it | Keeps `leaks.yml` a verbatim copy. |
| `leaks.yml` is copied from `3ad35c7`, not engineering-standards main | Main's copy also runs engineering-standards' own hook tests, which buzai does not have (rmorison/engineering-standards#109). |
| `ci.yml` installs gitleaks too, through the same script | Its required `checks` job runs the pre-commit hooks, and the leak-gate hook refuses to run without gitleaks. The script stays the only place the version is pinned. |
| `.pre-commit-config.yaml` sets `default_stages: [pre-commit]` | Installing the pre-push hook type would otherwise run ruff and the other hygiene hooks at push too. |
| Shape rules check file contents only | `leak-gate.sh` is copied verbatim, and it checks commit messages, paths and ref names against the value list alone. |
| The release check scans the commits since the last release, not all history (`docs/dev/RELEASING.md`) | Commits from before the gate's adoption hold committed-rule findings: invented test values and paths since rewritten. |
| Release tags are pushed one at a time, by name (`docs/dev/RELEASING.md`) | The pre-commit framework hands a pre-push hook only one ref of a push. |
| No `CODEOWNERS` on the gate files | In a one-person repository a required review never happens; the lead checks gate-file changes in the pull request diff instead. |

## Standing rules

| Content | Rule |
|---|---|
| Product code, docs, engineering history (plans, brainstorms, solutions, decisions) | ship — write public-safe at authoring time |
| Personal state (`audit/`, `.buzai/`, `hubs/*`, `deploy/env/*.env`, `notes/`, `.claude/settings.local.json`) | never (gitignored) |
| The private value list | **never in any repo** — lives on the maintainer's machine only |
| Workflows with write-scoped tokens or repo secrets in triggers | never without a security review |

## Authoring note

Development happens in the open. Write plans, brainstorms, and solution docs
public-safe from the start: no real hostnames or box identifiers (say "the test
box"), no personal contact details, no private-notes references. The gate catches
known values and shapes; it cannot catch a novel sensitive detail the first time —
that's an authoring habit, not a tool guarantee. A doc that describes a rule is
scanned like any other file, so describe what a rule matches by its shape, never
with a matching example.
