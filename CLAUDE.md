# Working in this repo

This repository is **public**. It is the system directory: machinery, docs, and
scaffolds. Every piece of personal state is an instantiation of a scaffold into a
location *outside* this checkout. Nothing personal is ever written in here.

## Where hubs live

Hubs — the principal's knowledge base — live at **`$BUZAI_HUBS_DIR`, defaulting to
`~/hubs`**, in a private git repository outside this checkout.

- Resolve that path with `scripts/hub_paths.py` (`hub_dir()`); never join `~/hubs`
  or guess a path yourself. It is the only place the location is computed.
- Print what it resolved to whenever you act on hubs, so a misconfiguration is
  visible rather than silent.
- The override belongs in the service unit, not a shell profile: a `--user` unit
  reads neither `.profile` nor `.bashrc`, so a profile-only value would silently
  point the assistant at a different store than the owner's.
- If the resolver refuses the configured path, stop and report it. Do not fall back
  to a guess.

**Never write hub content inside this checkout.** `hubs/` here holds tracked
scaffolds only. A hub file written here is one `git add` away from a public remote,
and `.gitignore` is not a barrier — the resolver refuses any path inside the
checkout for the same reason.

## Commit discipline for hub changes

- **One commit per logical hub change.** Not one per session, not one per file
  batch — one per thing that could be approved or rejected on its own.
- **The message must be reviewable without the diff.** State what changed and why,
  in plain language, naming the fact rather than the file: the owner reviews these
  from a phone, without the hunk in front of them.
- **Every commit carries a trailer naming the instruction source** — whether the
  change was made autonomously, at the owner's direction, or as a correction to an
  earlier change.
- Assistant-authored commits use the assistant's own author identity, so history
  distinguishes them from the owner's edits at a glance.

## The review loop

The owner drives review conversationally, from the Claude apps. There is no other
surface.

- When asked what changed, report the **undisposed** changes in plain language:
  what was recorded and why. Never paste diffs.
- The owner approves or rejects **per item**. Items not acted on stay pending —
  never advance them because the rest of the batch was approved.
- **A rejection removes the content**, unless the owner supplies a correct value, in
  which case it replaces it. Do not invent a replacement, re-derive the fact, or make
  a cosmetic edit and call it corrected.
- Record the owner's reason with the correction, so both the original and its
  resolution stay in history.

Ask `scripts/hub_review.py` for the pending list and record each verdict through it.
Never edit a hub file by hand to resolve a rejection: corrections go through the same
write path as every other hub change, so they get the same lock, scan, and provenance.

### Summarizing

Lead with the count, then read the items out as facts — newest first, numbered so the
owner can answer by number. For each one: what was recorded, why it was recorded, and
how long ago. Never paste the diff, the file, or the commit id unless asked; the owner
is judging the fact, not the change. If the summary opens with a warning about
unpushed history, say that first and plainly — it means the record exists only on
this box, and it usually means a credential expired.

### Asking

Ask for a verdict per item and treat silence as silence. An item the owner did not
answer is still pending and will come up again; that is the intended behavior, not an
oversight to tidy up. Never read "looks fine" as approval of a whole batch — say which
items you are about to settle, and settle only those.

### Correcting

A rejection needs the owner's reason in their own words. Ask for it if it was not
given: it is the only account of why the content went away, and it is what the
correction records.

Ask whether the fact should be **removed** or **replaced**, and if replaced, ask the
owner for the value. Do not propose one. The session that wrote the fact is gone and
its source material with it, so anything you supply is invented — and an invented
correction is worse than the original error, because it carries the owner's approval.

If the recorded text is no longer in the file — reworded or superseded by a later
change — say so, record the rejection anyway, and offer to review the change that
replaced it. Do not guess which of the current lines descended from the rejected one.

### Where dispositions live

Verdicts are stored per change and travel with the hub repo on their own ref, which
git does not push or fetch by default. The verbs handle the push. A restore onto a
fresh instance needs one extra fetch of the notes refs, so confirm that past verdicts
came back before calling a restore complete — otherwise every settled item resurfaces
as pending and the owner reviews their whole history again.

## Keeping this file public-safe

This file is tracked in a public repository. It states conventions only. No paths
outside the documented defaults, no hostnames, no account names, no instance-specific
detail — those belong in the private hub store or in local configuration.
