# Try it — make the safety features fire

The fastest way to trust the trust gate is to watch it work. These are short experiments
you run from the Claude app (or `claude.ai/code`) once your instance is attached. Each one
triggers a specific safety behavior, tells you what you should see, and shows you how to
confirm it in the audit log.

> **One rule for these experiments:** when the gate asks for approval, pick **"Yes"** —
> *not* "Yes, and don't ask again." The "don't ask again" option writes a native
> Claude Code allow-rule that **bypasses the trust gate** for that tool. For these
> experiments, always approve the single action so the gate stays in charge. (If you want a tool to run
> without asking, do it through the gate's own policy — see [`TUNING.md`](TUNING.md).)

You'll read the audit log a few times. On the server (as the instance's user):

```bash
tail -n 5 ~/buzai/audit/audit.jsonl | python3 -m json.tool
```

Each entry shows the `tool`, the `tier` (OWNER), the `legs` it touched
(`private_read` / `untrusted_content` / `external_send`), and the `decision`.

---

## 1. A normal read → it asks, you approve

**Type:** *"What's on my calendar this week?"*

**You should see:** an approval prompt before it reads your calendar ("requires confirmation
… approval required"). Approve it; you get your week.

**Why:** reading private data is gated by default (`review` → ask). Check the log — the
calendar read shows `legs: ["private_read", "untrusted_content"]` (calendar invites carry
attendee-supplied text, treated as untrusted) and `decision: "ask"`.

## 2. An external action → it asks before sending

**Type:** *"Create a calendar event tomorrow at 3pm titled 'gate test'."*

**You should see:** a prompt before the event is created. Approve it.

**Why:** creating an event notifies attendees / touches shared state — an **external send**.
The log shows `legs: ["external_send"]`. Note it carries *only* the send leg, not the
earlier read — that's the per-turn scoping at work (experiment 4 shows why that matters).

## 3. The headline: a risky combination → a loud "are you sure?"

**Type (all in one prompt):** *"Read my most recent email and draft a calendar invite to
the sender about it."*

**You should see:** the assistant reads the email (untrusted content) and goes to create an
invite (external send) — and the gate raises a **loud, explicit approval** that names all
three legs: *"this turn holds private read + untrusted content + external send; untrusted
content you've read may be steering this — approve only if you recognize and intend it."*

**Why:** this is the lethal-trifecta defense. In one turn the assistant combined reading
your data, ingesting attacker-influenceable content, and an external action. Rather than
silently doing it (dangerous) or hard-refusing (annoying), it escalates to *you* with the
risk spelled out. Approve or decline — you're the judge. In the log this approval is tagged
`trifecta_warning: true`, distinct from an ordinary ask, so risky approvals are auditable.

> Want to feel the protection? Decline it, then ask for the same invite as a fresh, separate
> prompt ("send X an invite for Y"). Now it's a clean send (no untrusted read in the same
> turn) and goes through as a normal approval — the gate blocked the *combination*, not the
> task.

## 4. Per-turn reset → it doesn't lock you out

**Type, as three separate prompts:**
1. *"Create a calendar event Friday at noon titled 'lunch'."* (approve — a send)
2. *"Search my email for anything from this week."* (approve — a read)
3. *"List my calendar for Friday."* (approve — a read)

**You should see:** each one simply asks and runs. None is blocked.

**Why:** a naive gate would accumulate the send leg from step 1 forever and then block every
later read as "the third leg." buzai resets the accounting at each new prompt, so a normal
day of unrelated requests never self-bricks. Confirm in the log: step 2's read shows
`legs: ["private_read", "untrusted_content"]` only — it did **not** inherit step 1's
`external_send`.

## 5. An unrecognized tool → it asks, never auto-runs

If you've connected a tool the shipped catalog doesn't classify (e.g. a finance connector
like Xero, or any connector you haven't added to the catalog yet):

**Type:** something that uses it, e.g. *"Show my latest financial summary."*

**You should see:** an approval prompt (not a silent run, and not a hard block).

**Why:** an unrecognized tool surfaces to you once rather than acting on its own — safe by
default. To give it the right behavior, classify it per [`TUNING.md`](TUNING.md).

## 6. Read your receipts

**On the server:**

```bash
tail -n 20 ~/buzai/audit/audit.jsonl | python3 -m json.tool   # the decisions you just made
.venv/bin/python -c "from trust import audit; print('chain intact:', audit.verify_chain('audit'))"
```

**You should see:** every decision from the experiments above, with its tier, legs, and
outcome — and `chain intact: True`. The log is hash-chained: if anyone edited a past entry,
`verify_chain` would return `False`. This is how you confirm what the assistant actually
did, independently of what it told you.

---

## What to send back

As you go, note: Did any prompt do something **without** asking that you expected to be
gated? Did a prompt get **blocked** that should have been fine? Was any approval prompt
unclear about *why* it was asking? Those are the most useful things to report. See
[`TUNING.md`](TUNING.md) if the gate is asking about something you'd rather it just did.
