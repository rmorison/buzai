# Releasing — maintainer notes

There is one public repo; releasing is ordinary git plus the gates.

## Every change

1. Work on a feature branch; Conventional Commits (`feat(make): …`, `docs: …`).
2. `pre-commit run --all-files` and `make test` locally (the pre-commit hooks include
   the secret scan and the publish gate — see `docs/dev/PUBLIC-SEED.md` for what the
   gate protects and where its private pattern file lives).
3. PR → CI green → merge.

## Cutting a release / notable milestone

1. Re-run the full gate with the private pattern file present:
   `.venv/bin/python scripts/publish_gate.py`
2. `make test && make trust-check` from a fresh clone.
3. Walk `docs/QUICKSTART.md` verbatim on a clean box (or reset account) when the
   installer or setup flow changed — the clean-box walkthrough is the release test.
4. Tag and push the one release tag by name:
   `git tag -a vX.Y.Z -m "…" && git push origin vX.Y.Z`.
   Versions are [semver](https://semver.org) `vX.Y.Z`, never `vX.Y`. Don't use
   `git push --tags`: the pre-push check scans only the first ref of a push, and
   `--tags` can also publish stray local tags.
5. Publish release notes for the tag (outline below).

## Release notes

Keep them short, written for someone who uses buzai rather than works on it:

- **Summary** — one or two sentences on what this release is for.
- **What's new** — user-visible features, one line each.
- **Fixes** — what was wrong and now isn't.
- **Upgrading** — anything an existing install must do (re-run a `make` verb,
  a changed default, a new setting), or "Nothing."
- **Security** — trust-gate or leak-gate changes, if any.

## Invariants

- `make` verb **names** are public API — never rename or remove; recipes may evolve.
- Nothing in `docs/dev/PUBLIC-SEED.md`'s "never" rows may be committed.
- The private gate-pattern file never enters any repo.
