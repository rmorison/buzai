# Releasing — maintainer notes

There is one public repo; releasing is ordinary git plus the gates.

## Every change

1. Branch from an issue as `{issue}-{slug}` (see `CONTRIBUTING.md`, "Branches and
   merging"); Conventional Commits (`feat(make): …`, `docs: …`).
2. `pre-commit run --all-files` and `make test` locally (the pre-commit hooks include
   the leak gate — see `docs/dev/PUBLIC-SEED.md` for what the gate protects and how
   the private value list is declared).
3. PR → CI green → the maintainer merges, as a merge commit.

## Cutting a release / notable milestone

Pick the release commit first, the head of `origin/main`, and note its SHA. Steps 1–3
run against that commit: `git checkout <sha>` in every checkout they use, the fresh
clone and the clean box included. Step 4 tags it by SHA, so a merge that lands
meanwhile can't slip into the release untested.

1. Run the leak gate's value rules over the release's commits, with the private
   value list declared: `sh scripts/leak-gate.sh range <previous tag>..<sha>`. When
   no release tag comes after the gate's adoption, start the range at the commit
   that merged its adoption (#28). Not `history`: commits from before the adoption
   hold committed-rule findings, invented test values and paths since rewritten,
   and would fail every release.
2. `make test && make trust-check` from a fresh clone.
3. Walk `docs/QUICKSTART.md` verbatim on a clean box (or reset account) when the
   installer or setup flow changed — the clean-box walkthrough is the release test.
4. Tag the release commit by SHA, and push the one release tag by name:
   `git tag -a vX.Y.Z <sha> -m "…" && git push origin vX.Y.Z`.
   Versions are [semver](https://semver.org) `vX.Y.Z`, never `vX.Y`. Don't use
   `git push --tags`: it can publish stray local tags, and pre-commit's pre-push
   stage hands its hooks only the first ref of a push. Naming the one tag keeps the
   leak gate's pre-push hook scanning the whole push.
5. Publish the release notes (outline below) as the GitHub release for the tag,
   writing the notes file outside the checkout:
   `gh release create vX.Y.Z --verify-tag --notes-file <path>`.
   `--verify-tag` fails if the tag didn't reach GitHub, rather than creating one
   on whatever `main` is there.

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
- The private value list never enters any repo.
