# Hubs — your knowledge base

Hubs are the assistant's long-lived personal knowledge base: one canonical
markdown file (or, once large, a folder) per life domain — finances, home,
career, a project, a trip. They are **your** content and are gitignored; the
template ships only this README and `_example-hub.md` as scaffolds.

## How hubs work

- **One hub per domain.** Start each as a single `<domain>.md` (copy
  `_example-hub.md`). When a hub gets large enough that reading it whole is
  wasteful, split it into a folder — an index plus per-topic entries with light
  frontmatter and `[[wikilinks]]`. Earn the split; don't pre-split small hubs.
- **The hub is the source of truth.** When a hub and an incoming brief disagree,
  the hub wins.
- **Markdown in git.** Hubs are plain files — diffable, reviewable, and openable
  in any editor (or Obsidian). No database, no extra infra.

## Getting started

```bash
cp hubs/_example-hub.md hubs/finance-and-tax.md   # then edit
```

Your real hub files (`hubs/*.md` other than this README and the example) are
gitignored — they never leave your machine via this repo.
