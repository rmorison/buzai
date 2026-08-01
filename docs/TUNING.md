# Tuning the gate — relax and tighten as you build trust

The trust gate ships **strict**: almost everything asks for approval. That's deliberate —
you should start by seeing what the assistant wants to do, then relax the gate for the
actions you trust, at your own pace. This is how you tune it. All of it is plain
configuration in `trust/config/`; no code. For the deeper reference see `trust/README.md`.

> **Keep your tuning personal and gitignored.** Every config file has a `*.local.toml`
> companion (e.g. `policy.local.toml`) that is **gitignored** and overrides the shipped
> defaults. Put *your* promotions and recipients there, not in the tracked files — so your
> personal trust choices never get shared or overwritten by an update.

## The standing policy — auto / notify / review

`trust/config/policy.toml` decides, per kind of action, whether it runs silently, runs and
tells you, or asks first:

- **`review`** — ask before acting. The default for anything not listed.
- **`notify`** — act, then report what it did with an undo affordance.
- **`auto`** — act silently, logged to the audit trail.

As you gain confidence in a recurring action, promote it. For example, in
`policy.local.toml`:

```toml
[classes]
# you've watched it append to your metrics sheet 20 times and it's always right:
sheet_append = "notify"
# routine, low-stakes, you never want to be asked:
create_task  = "auto"
```

Read-only inspection of your own workspace already ships as `auto` (it doesn't prompt for
reads), which matches Claude Code's normal behavior — but those reads still count toward
the trifecta, so a later risky combination is still caught.

## Classifying your connectors

The gate only reasons correctly about a connector if it knows what that connector's tools
**touch**. `trust/config/connector-legs.toml` maps each tool to its trifecta "legs"
(`private_read`, `untrusted_content`, `external_send`). It ships:

- **Active** presets for the common managed connectors (Gmail, Calendar, Drive, Todoist,
  Dropbox), with their real tool names.
- **Commented** presets for ~13 directory connectors (Slack, Notion, GitHub, …). To enable
  one: add the connector, run `/mcp` to read its **real** tool names, then uncomment its
  block and fix the `match` patterns to those names.

**The classification rubric** (be conservative — a mislabel is a security hole):

- A read of external or shared content (mail, issues, shared docs, invites) →
  `private_read` + `ingests_untrusted`.
- Any action others can see (post, comment, create, assign, share) → `external_send`.
- Destructive or irreversible actions (delete, money, share-links) → **hard-gate** them
  (see below).

If you connect something the catalog doesn't cover, the gate still protects you — an
unclassified tool simply **asks** every time. Classifying it just gives it the right
behavior (and lets you promote it).

## Always-gate — actions that must always ask

`trust/config/always-gate.toml` is a short list of high-blast-radius action classes that
**always** require approval, regardless of tier or any policy promotion: money movement,
new-recipient sends, destructive deletes, share-links, granting new access. These can't be
promoted away — that's the point. Add your own irreversible operations here.

## Known recipients — stop gating sends to people you trust

A send to a **new** recipient always asks (so injected content can't quietly reach a new
address). Once you've confirmed a recipient is safe, add them to
`trust/config/known-recipients.local.toml` and sends to them stop tripping the
new-recipient gate:

```toml
recipients = ["teammate@example.com", "myself@example.com"]
```

The known-recipients file is itself protected — the assistant can't pre-seed it to dodge
the gate.

## A sane tuning path

1. **Start strict** (the shipped defaults). Run the [`TRY-IT.md`](TRY-IT.md) experiments and
   a day or two of real use; watch what asks.
2. **Promote the boring stuff** to `notify`, then `auto`, in `policy.local.toml` — one
   action class at a time, only after you've seen it behave.
3. **Add trusted recipients** as they come up.
4. **Leave the always-gate list alone** unless you have a specific irreversible action to
   add. The whole point is that some things should always pause for a human.
5. **Check the audit log** (`audit/audit.jsonl`) after changes to confirm the gate is doing
   what you expect — see [`TRY-IT.md`](TRY-IT.md) §6.

Promotions are reversible: tighten anything back to `review` the moment it surprises you.
