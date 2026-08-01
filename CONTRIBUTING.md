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
make dev         # installs the pre-commit hooks (hygiene, ruff, secret scan, leak gate)
make test        # full suite must pass
```

- **Conventional Commits** — `feat(trust): …`, `fix(make): …`, `docs: …`.
- **Stdlib only.** The Python here deliberately has zero third-party runtime
  dependencies. PRs adding one need a very good reason.
- **`make` verb names are public API.** Never rename or remove a verb; recipes may
  evolve. New verbs need a `## ` help comment.
- **Fail-closed by default.** Anything touching the trust gate keeps the conservative
  default (unknown → ask); see `docs/SECURITY-MODEL.md` for the model you're preserving.
- New behavior needs tests (`trust/tests/`, `scripts/tests/` — plain `unittest`).

## Reporting problems

Bugs and friction → GitHub issues. Security vulnerabilities → **privately**, per
[`SECURITY.md`](SECURITY.md).
