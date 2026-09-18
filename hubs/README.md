# Hubs — scaffolds live here, your content does not

Hubs are the assistant's long-lived personal knowledge base: one canonical
markdown file (or, once large, a folder) per life domain — finances, home,
career, a project, a trip.

**This directory is the template.** It ships the scaffolds the public repo tracks —
this README and `_example-hub.md` — and nothing else. Your real hubs live in a
private git repository *outside* this checkout, so a `git pull` can update the
system directory with no possibility of trampling personal content, and no
possibility of personal content reaching the public remote.

## Where your hubs actually live

`$BUZAI_HUBS_DIR`, defaulting to `~/hubs`. That location is a private git
repository: your content, versioned, with every assistant-made change committed
with a message you can review in plain language.

`scripts/hub_paths.py` is the one resolver — the assistant, the scripts, and the
service preflight all ask it rather than assuming a path. It refuses any location
inside this checkout, following symlinks first, so a hub store can't be quietly
aimed back at the public repo.

```bash
.venv/bin/python scripts/hub_paths.py          # print where hubs resolve to, and why
BUZAI_HUBS_DIR=/srv/hubs .venv/bin/python scripts/hub_paths.py
```

Set the override in the service unit, not a shell profile — a `--user` unit reads
neither `.profile` nor `.bashrc`, so a profile-only value would point the assistant
at a different store than yours.

## How hubs work

- **One hub per domain.** Start each as a single `<domain>.md` (copy
  `_example-hub.md` into your hub store). When a hub gets large enough that reading
  it whole is wasteful, split it into a folder — an index plus per-topic entries
  with light frontmatter and `[[wikilinks]]`. Earn the split; don't pre-split small
  hubs.
- **The hub is the source of truth.** When a hub and an incoming brief disagree,
  the hub wins.
- **Markdown in git.** Hubs are plain files — diffable, reviewable, and openable
  in any editor (or Obsidian). No database, no extra infra.

## Getting started

Run `make hub-init` once: it creates the private hub store outside this checkout,
seeds `_example-hub.md` into it, and moves out any hub content that is already sitting
here. (Interrupted half-way? Run it again — it resumes.)

After that, hub content is written by **one** tool, `scripts/hub_commit.py`, which
takes the write lock, scans for credentials, and records one reviewable commit:

```bash
.venv/bin/python scripts/hub_commit.py --file finance-and-tax.md \
  --append "- Filed 2025 return on 2026-03-02" --section "Tax" \
  --summary "Record when the 2025 return was filed" --source owner-directed
```

Naming a file that does not exist yet is how a new hub starts — no `cp` needed, and
no file is ever created by hand: a hub file written directly bypasses the lock, the
credential scan, and the provenance trail the review loop depends on. Copy
`_example-hub.md` only to see the shape a hub takes.
