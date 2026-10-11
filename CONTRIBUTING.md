# Contributing to buzai

Thanks for helping make always-on assistants safer. A few ground rules keep this
easy for everyone.

## Inbound license & sign-off (DCO)

Contributions are accepted under the project's [MIT license](LICENSE)
(inbound = outbound). Every commit must carry a
[Developer Certificate of Origin](https://developercertificate.org/) sign-off —
your assertion that you wrote the change or have the right to submit it:

```bash
git commit -s
```

That adds a `Signed-off-by: Your Name <you@example.com>` line; the DCO check on PRs
verifies it. No CLA.

## Before you open a PR

```bash
make venv        # pinned interpreter, zero third-party deps
make dev         # installs gitleaks + the pre-commit and pre-push hooks (hygiene, ruff, leak gate)
make test        # full suite must pass
```

The hooks run the leak gate with the first `gitleaks` on your `PATH`: keep
`~/.local/bin`, where `make dev` installs the pinned one, on it, or set `GITLEAKS`.
Without a private value list you get the committed rules and a one-line note; see
`docs/dev/PUBLIC-SEED.md`.

- **Conventional Commits** — `feat(trust): …`, `fix(make): …`, `docs: …`.
- **Stdlib only.** The Python here deliberately has zero third-party runtime
  dependencies. PRs adding one need a very good reason.
- **`make` verb names are public API.** Never rename or remove a verb; recipes may
  evolve. New verbs need a `## ` help comment.
- **Fail-closed by default.** Anything touching the trust gate keeps the conservative
  default (unknown → ask); see `docs/SECURITY-MODEL.md` for the model you're preserving.
- New behavior needs tests (`trust/tests/`, `scripts/tests/` — plain `unittest`).
- **Never name a dev checkout `buzai-assistant`.** That is the deployment's directory,
  and the Remote Control picker tells environments apart by directory name; see
  `CONCEPTS.md`, "Deployment directory".

## Branches and merging

- **Branch from an issue.** Every change traces to an issue. Use the branch name
  GitHub generates from the issue, `{issue}-{slug}` (for example
  `12-branching-and-release-docs`). Working from a fork, create the branch by hand
  with the same name.
- **Merge commits, on purpose.** Pull requests land as merge commits, not squashes.
  The per-commit history is part of the project's engineering trail: each commit
  records one step and why it was taken. A squash folds the branch into a single
  commit and that record goes with it.
- **The maintainer merges.** Open the PR and get CI green; the maintainer reviews
  and merges it.

## For Maintainers

Before a pull request merges, a maintainer runs the **private value rules** over
its commits. Beyond the committed rules that CI runs, they check file contents,
file paths, commit messages and author identities against the maintainer's value
list, which lives outside every repository and never reaches CI.

**Who runs it.**

- **A pull request from an agent worker:** the team's lead runs it as part of its
  Done gate, and the ready-to-merge comment records the result.
- **A pull request from an outside contributor:** the maintainer runs it. Those
  commits never passed through a value list.
- **Commits pushed from a maintainer's own machine** already passed the pre-push
  check that `make dev` installs, on a machine with the hooks installed and a list
  declared. For those this run is a backstop.

**How.** Run it from the default branch's own checkout, never the pull request's.
The pull request can edit the gate script and its committed rules, so the branch
under review must not supply its own rules. With `<N>` the pull request number:

```bash
git switch main
git pull --ff-only
git fetch origin pull/<N>/head:refs/leakgate/pr-<N>
sh scripts/leak-gate.sh range origin/main..refs/leakgate/pr-<N>
```

It exits 0 when clean and 1 on a finding, which it reports by the list's line
number, never the value. It exits 2 on a usage, list or gitleaks error. Read the
output as well as the exit code: if it says value rules were skipped, the run
checked only the committed rules and is not a pre-merge check. Delete the
ref afterwards with `git update-ref -d refs/leakgate/pr-<N>`.

**Setup it assumes.**

- The value list is declared once per machine, by path. The documented default
  location is `~/.config/leakgate/values`:

  ```bash
  git config --global leakgate.values ~/.config/leakgate/values
  ```

  With no declaration, or a clone that opts out with `leakgate.values none`, the
  run skips the value rules and says so. The list's format and the rule
  categories are in `docs/dev/PUBLIC-SEED.md`. Never write a value into this
  repository, an issue or a pull request.
- gitleaks is installed by `make dev`, as in "Before you open a PR".

## Reporting problems

Bugs and friction → GitHub issues. Security vulnerabilities → **privately**, per
[`SECURITY.md`](SECURITY.md).
