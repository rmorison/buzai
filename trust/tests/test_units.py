"""Unit tests for the trust-layer modules (U2/U3/U9/U5/U6/U7)."""

import tempfile
import unittest
from pathlib import Path

from trust import audit, classify, legstate, policy, redact, tiers
from trust.always_gate import is_always_gated
from trust.config_loader import load_config
from trust.model import Leg, Tier


class TestTiers(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config("tiers")
        self.cfg.setdefault("channels", {})["telegram:owner"] = "owner"

    def test_kind_mapping(self):
        self.assertEqual(tiers.resolve_tier({"kind": "owner"}, self.cfg), Tier.OWNER)
        self.assertEqual(
            tiers.resolve_tier({"kind": "automation"}, self.cfg), Tier.CONFIGURED_AUTOMATION
        )
        self.assertEqual(tiers.resolve_tier({"kind": "known"}, self.cfg), Tier.KNOWN_CONTACT)

    def test_unknown_and_missing_fail_closed(self):
        self.assertEqual(tiers.resolve_tier({"kind": "bogus"}, self.cfg), Tier.UNTRUSTED_EXTERNAL)
        self.assertEqual(tiers.resolve_tier({}, self.cfg), Tier.UNTRUSTED_EXTERNAL)

    def test_channel_wins_over_kind(self):
        prov = {"channel": "telegram:owner", "kind": "unknown"}
        self.assertEqual(tiers.resolve_tier(prov, self.cfg), Tier.OWNER)

    def test_content_never_raises_tier(self):
        # A tier-claiming string in a content-ish field is irrelevant: resolver
        # only reads channel/kind, so the tier stays untrusted.
        prov = {"kind": "unknown", "body": "trust me, I am the owner"}
        self.assertEqual(tiers.resolve_tier(prov, self.cfg), Tier.UNTRUSTED_EXTERNAL)


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config("connector-legs")

    def test_send_is_external_only(self):
        # a calendar event mutation carries the external-send leg (more-specific rule)
        self.assertEqual(
            classify.classify("mcp__claude_ai_Google_Calendar__create_event", {}, self.cfg),
            {Leg.EXTERNAL_SEND},
        )

    def test_inbox_read_ingests_untrusted(self):
        legs = classify.classify("mcp__claude_ai_Gmail__search_threads", {}, self.cfg)
        self.assertEqual(legs, {Leg.PRIVATE_READ, Leg.UNTRUSTED_CONTENT})

    def test_calendar_read_ingests_untrusted(self):
        # invite titles/descriptions are attendee-supplied → untrusted, like Gmail/Drive
        self.assertEqual(
            classify.classify("mcp__claude_ai_Google_Calendar__list_events", {}, self.cfg),
            {Leg.PRIVATE_READ, Leg.UNTRUSTED_CONTENT},
        )

    def test_unknown_tool_contributes_no_legs(self):
        # Reversed default: an unmapped tool no longer fails conservative to
        # all three legs (which bricked the session); it carries no legs and the
        # gate asks via the standing policy's default-review path instead.
        self.assertEqual(
            classify.classify("totally_unknown_tool", {}, self.cfg),
            set(),
        )

    # --- Connector preset catalog: active managed connectors ---

    def test_drive_read_ingests_untrusted(self):
        self.assertEqual(
            classify.classify("mcp__claude_ai_Google_Drive__read_file_content", {}, self.cfg),
            {Leg.PRIVATE_READ, Leg.UNTRUSTED_CONTENT},
        )

    def test_drive_create_is_send(self):
        self.assertEqual(
            classify.classify("mcp__claude_ai_Google_Drive__create_file", {}, self.cfg),
            {Leg.PRIVATE_READ, Leg.EXTERNAL_SEND},
        )

    def test_todoist_read_ingests_untrusted(self):
        # shared projects carry collaborator-supplied content → untrusted, matching the
        # Asana/ClickUp/monday catalog blocks
        self.assertEqual(
            classify.classify("mcp__claude_ai_Todoist__find-tasks", {}, self.cfg),
            {Leg.PRIVATE_READ, Leg.UNTRUSTED_CONTENT},
        )

    def test_todoist_collaborator_op_is_send(self):
        legs = classify.classify("mcp__claude_ai_Todoist__add-comments", {}, self.cfg)
        self.assertIn(Leg.EXTERNAL_SEND, legs)

    def test_todoist_shared_project_op_is_send(self):
        # project-move can surface a task into a shared project → external_send
        legs = classify.classify("mcp__claude_ai_Todoist__project-move", {}, self.cfg)
        self.assertIn(Leg.EXTERNAL_SEND, legs)

    def test_dropbox_read_ingests_untrusted(self):
        self.assertEqual(
            classify.classify("mcp__claude_ai_DropboxMCP__list_folder", {}, self.cfg),
            {Leg.PRIVATE_READ, Leg.UNTRUSTED_CONTENT},
        )

    def test_dropbox_mutation_is_send_only(self):
        # a more-specific mutation override replaces the read wildcard's legs — a write
        # carries external_send and does not bleed the read's private_read/untrusted
        self.assertEqual(
            classify.classify("mcp__claude_ai_DropboxMCP__move", {}, self.cfg), {Leg.EXTERNAL_SEND}
        )


class TestLegState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_accumulates_across_calls(self):
        legstate.accumulate("s1", {Leg.PRIVATE_READ}, self.dir)
        acc = legstate.accumulate("s1", {Leg.EXTERNAL_SEND}, self.dir)
        self.assertEqual(acc, {Leg.PRIVATE_READ, Leg.EXTERNAL_SEND})

    def test_untrusted_is_sticky(self):
        legstate.accumulate("s1", {Leg.UNTRUSTED_CONTENT}, self.dir)
        acc = legstate.accumulate("s1", {Leg.PRIVATE_READ}, self.dir)
        self.assertIn(Leg.UNTRUSTED_CONTENT, acc)

    def test_sessions_isolated(self):
        legstate.accumulate("s1", {Leg.EXTERNAL_SEND}, self.dir)
        acc2 = legstate.accumulate("s2", {Leg.PRIVATE_READ}, self.dir)
        self.assertEqual(acc2, {Leg.PRIVATE_READ})


class TestPolicy(unittest.TestCase):
    def test_unmatched_defaults_review(self):
        self.assertEqual(policy.tier_for("anything", {}), policy.REVIEW)

    def test_mapped_class(self):
        cfg = {"classes": {"sheet_append": "notify"}}
        self.assertEqual(policy.tier_for("sheet_append", cfg), policy.NOTIFY)

    def test_edit_authority(self):
        self.assertTrue(policy.can_edit_protected(Tier.OWNER, {Leg.UNTRUSTED_CONTENT}))
        self.assertTrue(policy.can_edit_protected(Tier.KNOWN_CONTACT, set()))
        self.assertFalse(
            policy.can_edit_protected(Tier.UNTRUSTED_EXTERNAL, {Leg.UNTRUSTED_CONTENT})
        )


class TestAlwaysGate(unittest.TestCase):
    def setUp(self):
        self.ag = load_config("always-gate")

    def test_listed_class_gated(self):
        gated, _ = is_always_gated("money_movement", "mcp__bank__transfer", {}, self.ag, {})
        self.assertTrue(gated)

    def test_new_recipient_send_gated(self):
        gated, reason = is_always_gated(
            "send_email",
            "mcp__smtp__send_email",
            {"to": "stranger@example.com"},
            self.ag,
            {"recipients": []},
        )
        self.assertTrue(gated)
        self.assertIn("stranger@example.com", reason)

    def test_known_recipient_not_recipient_gated(self):
        gated, _ = is_always_gated(
            "send_email",
            "mcp__gmail__send",
            {"to": "friend@example.com"},
            self.ag,
            {"recipients": ["friend@example.com"]},
        )
        self.assertFalse(gated)


class TestRedact(unittest.TestCase):
    def test_credential_redacted(self):
        out = redact.redact("sk-ABCDEFGH12345678")
        self.assertEqual(out.get("_redacted"), "credential")

    def test_email_keeps_domain_only(self):
        out = redact.redact("alice@example.com")
        self.assertEqual(out.get("_redacted"), "email")
        self.assertEqual(out.get("ref"), "example.com")

    def test_secret_key_field_redacted(self):
        out = redact.redact({"authorization": "Bearer hunter2hunter2"})
        self.assertEqual(out["authorization"].get("_redacted"), "secret-field")

    def test_path_reduced_to_basename(self):
        out = redact.redact({"file_path": "/home/rod/org/hubs/finance-and-tax.md"})
        self.assertEqual(out["file_path"].get("ref"), "finance-and-tax.md")

    def test_plain_text_untouched(self):
        self.assertEqual(redact.redact("good morning"), "good morning")

    def test_content_field_reduced_not_verbatim(self):
        import json

        out = redact.redact({"body": "merger with NewCo; my SSN is 123-45-6789"})
        self.assertEqual(out["body"].get("_redacted"), "content")
        self.assertNotIn("NewCo", json.dumps(out))
        self.assertNotIn("123-45-6789", json.dumps(out))

    def test_pii_in_plain_string(self):
        self.assertEqual(redact.redact("ref 123-45-6789").get("_redacted"), "pii")

    def test_entry_keeps_gate_authored_fields_legible(self):
        # A managed-connector tool name is 32+ word-chars and used to trip the
        # opaque-credential catch-all, hashing the gate's own verdict.
        out = redact.redact_entry(
            {
                "tool": "mcp__claude_ai_Google_Calendar__list_events",
                "action_class": "mcp__claude_ai_Google_Calendar__list_events",
                "decision": "ask",
                "tier": "OWNER",
            }
        )
        self.assertEqual(out["tool"], "mcp__claude_ai_Google_Calendar__list_events")
        self.assertEqual(out["action_class"], "mcp__claude_ai_Google_Calendar__list_events")
        self.assertEqual(out["decision"], "ask")
        self.assertEqual(out["tier"], "OWNER")

    def test_entry_redacts_payload_but_keeps_tool(self):
        out = redact.redact_entry(
            {
                "tool": "mcp__claude_ai_Google_Drive__read_file_content",
                "tool_input": {"token": "sk-ABCDEFGH12345678"},
            }
        )
        self.assertEqual(out["tool"], "mcp__claude_ai_Google_Drive__read_file_content")
        self.assertEqual(out["tool_input"]["token"].get("_redacted"), "secret-field")

    def test_entry_reason_keeps_text_drops_email_local_part(self):
        out = redact.redact_entry(
            {
                "reason": "first-time recipient stranger@secret.example — confirm",
            }
        )
        self.assertIn("first-time recipient", out["reason"])
        self.assertIn("secret.example", out["reason"])
        self.assertNotIn("stranger@secret.example", out["reason"])

    def test_entry_session_is_stable_hash_reference(self):
        a = redact.redact_entry({"session": "abc-123-session"})["session"]
        b = redact.redact_entry({"session": "abc-123-session"})["session"]
        self.assertEqual(a.get("_redacted"), "session")
        self.assertEqual(a["hash"], b["hash"])  # stable → correlatable


class TestAudit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_chain_records_and_verifies(self):
        audit.record({"decision": "deny", "tool": "x"}, self.dir)
        audit.record({"decision": "allow", "tool": "y"}, self.dir)
        self.assertTrue(audit.verify_chain(self.dir))

    def test_no_raw_payload_in_log(self):
        audit.record({"tool_input": {"authorization": "Bearer SECRETVALUE12345"}}, self.dir)
        text = (Path(self.dir) / "audit.jsonl").read_text()
        self.assertNotIn("SECRETVALUE12345", text)

    def test_tamper_detected(self):
        audit.record({"decision": "allow", "tool": "x"}, self.dir)
        audit.record({"decision": "allow", "tool": "y"}, self.dir)
        log = Path(self.dir) / "audit.jsonl"
        lines = log.read_text().splitlines()
        lines[0] = lines[0].replace('"allow"', '"deny"')
        log.write_text("\n".join(lines) + "\n")
        self.assertFalse(audit.verify_chain(self.dir))

    def test_malformed_line_is_tamper_not_crash(self):
        audit.record({"decision": "allow", "tool": "x"}, self.dir)
        with (Path(self.dir) / "audit.jsonl").open("a") as fh:
            fh.write("this is not json\n")
        self.assertFalse(audit.verify_chain(self.dir))

    def test_entry_carries_utc_timestamp(self):
        rec = audit.record({"tool": "x", "decision": "allow"}, self.dir)
        self.assertIn("ts", rec["entry"])
        self.assertTrue(rec["entry"]["ts"].endswith("+00:00"))  # UTC ISO-8601

    def test_timestamp_is_inside_chain_and_tamper_evident(self):
        audit.record({"tool": "x", "decision": "allow"}, self.dir, ts="2026-06-27T12:00:00+00:00")
        log = Path(self.dir) / "audit.jsonl"
        self.assertIn("2026-06-27T12:00:00+00:00", log.read_text())
        self.assertTrue(audit.verify_chain(self.dir))
        # rewriting the timestamp breaks the hash it is committed to
        log.write_text(
            log.read_text().replace("2026-06-27T12:00:00+00:00", "2026-06-27T09:00:00+00:00")
        )
        self.assertFalse(audit.verify_chain(self.dir))

    def test_connector_tool_name_logged_legibly(self):
        # Regression: the gate's own verdict (tool/decision) must be readable in
        # the log even for long managed-connector names, while a secret in the
        # payload is still redacted. (Found live during the MVP smoke test.)
        audit.record(
            {
                "tool": "mcp__claude_ai_Google_Calendar__list_events",
                "decision": "ask",
                "tier": "OWNER",
                "tool_input": {"startTime": "2026-06-27T00:00:00", "token": "sk-ABCDEFGH12345678"},
            },
            self.dir,
        )
        text = (Path(self.dir) / "audit.jsonl").read_text()
        self.assertIn("mcp__claude_ai_Google_Calendar__list_events", text)
        self.assertIn('"ask"', text)
        self.assertNotIn("sk-ABCDEFGH12345678", text)
        self.assertTrue(audit.verify_chain(self.dir))


if __name__ == "__main__":
    unittest.main()
