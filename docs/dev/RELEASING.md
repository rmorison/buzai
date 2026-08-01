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
4. Tag: `git tag -a vX.Y -m "…" && git push --tags`.

## Invariants

- `make` verb **names** are public API — never rename or remove; recipes may evolve.
- Nothing in `docs/dev/PUBLIC-SEED.md`'s "never" rows may be committed.
- The private gate-pattern file never enters any repo.
