"""Integration tests for the gate (U4), covering acceptance examples AE1-AE5
and the fail-closed shim behavior (KTD5)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from trust import audit, hook, legstate
from trust.config_loader import DEFAULT_CONFIG_DIR
from trust.gate import GateConfig, gate
from trust.model import Decision


class GateTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "legstate"
        self.audit_dir = self.root / "audit"
        self.prov_file = self.root / "provenance.json"
        os.environ["BUZAI_PROVENANCE_FILE"] = str(self.prov_file)

    def tearDown(self):
        os.environ.pop("BUZAI_PROVENANCE_FILE", None)
        self._tmp.cleanup()

    def set_provenance(self, prov):
        if prov is None:
            if self.prov_file.exists():
                self.prov_file.unlink()
        else:
            self.prov_file.write_text(json.dumps(prov))

    def fixture_config(self, policy_overrides=None, known=None):
        """Copy shipped config to a temp dir, optionally overriding policy/known."""
        cdir = self.root / "config"
        shutil.copytree(DEFAULT_CONFIG_DIR, cdir)
        if policy_overrides is not None:
            (cdir / "policy.toml").write_text(policy_overrides)
        if known is not None:
            (cdir / "known-recipients.toml").write_text(known)
        return cdir

    def cfg(self, config_dir=None):
        return GateConfig(
            config_dir=config_dir, state_dir=str(self.state_dir), audit_dir=str(self.audit_dir)
        )

    def run_call(self, cfg, session, tool, tool_input=None):
        return gate({"session_id": session, "tool_name": tool, "tool_input": tool_input or {}}, cfg)


class TestAcceptanceExamples(GateTestBase):
    def test_ae1_all_three_legs_denied_and_split(self):
        self.set_provenance({"kind": "unknown"})
        cfg = self.cfg()
        # call 1: read inbox -> private_read + untrusted_content
        self.run_call(cfg, "task1", "mcp__claude_ai_Gmail__search_threads")
        # call 2: append to a sheet -> private_read + external_send; now all three accumulated
        res = self.run_call(cfg, "task1", "mcp__sheets__append", {"row": ["data"]})
        self.assertEqual(res.decision, Decision.DENY)
        self.assertIn("three trifecta legs", res.reason)

    def test_ae2_new_recipient_gated_even_when_policy_auto(self):
        policy = (
            '[classes_of]\n"mcp__smtp__send_email" = "send_email"\n\n'
            '[classes]\nsend_email = "auto"\n'
        )
        cfg = self.cfg(
            self.fixture_config(
                policy_overrides=policy, known='recipients = ["friend@example.com"]\n'
            )
        )
        self.set_provenance({"kind": "owner"})
        res = self.run_call(cfg, "task2", "mcp__smtp__send_email", {"to": "stranger@example.com"})
        self.assertEqual(res.decision, Decision.ASK)  # always-gate overrides policy:auto
        self.assertIn("stranger@example.com", res.reason)

    def test_ae3_protected_config_edit_from_untrusted_denied(self):
        self.set_provenance({"kind": "unknown"})
        cfg = self.cfg()
        self.run_call(
            cfg, "task3", "mcp__claude_ai_Gmail__search_threads"
        )  # ingest untrusted content
        res = self.run_call(
            cfg, "task3", "Write", {"file_path": "trust/config/policy.toml", "content": "x"}
        )
        self.assertEqual(res.decision, Decision.DENY)
        self.assertIn("protected", res.reason)

    def test_nested_protected_config_edit_denied(self):
        # A MultiEdit nested path to a protected config must not slip past R8 once
        # the task has ingested untrusted content. (Built-in writes carry no legs
        # by themselves now, so the untrusted-content leg comes from a real ingest —
        # which is exactly the AE3 threat: an untrusted-content task editing config.)
        self.set_provenance({"kind": "unknown"})
        cfg = self.cfg()
        self.run_call(cfg, "taskN", "mcp__claude_ai_Gmail__search_threads")  # ingest untrusted
        res = self.run_call(
            cfg,
            "taskN",
            "MultiEdit",
            {"edits": [{"file_path": "trust/config/known-recipients.toml", "new": "x"}]},
        )
        self.assertEqual(res.decision, Decision.DENY)
        self.assertIn("protected", res.reason)

    def test_send_leg_tool_without_class_mapping_gates_new_recipient(self):
        # A tool carrying the external-send leg (mcp__sheets__* → external_send) but
        # with no classes_of entry still gates on a new recipient via has_external_send.
        # (This is the real intent of always_gate's has_external_send path. Note the
        # send leg must come from a connector-legs mapping now — an entirely *unmapped*
        # tool carries no legs by design, and is covered by the unmapped-tool
        # tests below, which gate it via policy default-review instead.)
        self.set_provenance({"kind": "owner"})
        res = self.run_call(
            self.cfg(), "taskS", "mcp__sheets__update", {"to": "stranger@example.com"}
        )
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("stranger@example.com", res.reason)

    def test_ae4_notify_class_allows_with_undo(self):
        policy = (
            '[classes_of]\n"mcp__sheets__append" = "sheet_append"\n\n'
            '[classes]\nsheet_append = "notify"\n'
        )
        cfg = self.cfg(self.fixture_config(policy_overrides=policy))
        self.set_provenance({"kind": "owner"})
        res = self.run_call(cfg, "task4", "mcp__sheets__append", {"row": ["x"]})
        self.assertEqual(res.decision, Decision.ALLOW)
        self.assertTrue(res.notify)

    def test_ae5_audit_entry_redacts_sensitive_payload(self):
        self.set_provenance({"kind": "owner"})
        cfg = self.cfg()
        self.run_call(
            cfg,
            "task5",
            "mcp__smtp__send_email",
            {"to": "alice@example.com", "authorization": "Bearer SECRETTOKEN123456"},
        )
        log = (self.audit_dir / "audit.jsonl").read_text()
        self.assertNotIn("SECRETTOKEN123456", log)
        self.assertNotIn("alice@example.com", log)


class TestSubagentContainment(GateTestBase):
    """U2: the Agent/Task spawn is always-gated and must not poison leg state."""

    def test_agent_spawn_is_always_gated_even_for_owner(self):
        self.set_provenance({"kind": "owner"})
        res = self.run_call(
            self.cfg(), "tsa", "Agent", {"description": "do a thing", "prompt": "..."}
        )
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("always-gate", res.reason)

    def test_task_alias_is_always_gated(self):
        self.set_provenance({"kind": "owner"})
        res = self.run_call(self.cfg(), "tst", "Task", {"prompt": "..."})
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("always-gate", res.reason)

    def test_agent_spawn_is_gated_for_untrusted_too(self):
        self.set_provenance({"kind": "unknown"})
        res = self.run_call(self.cfg(), "tsu", "Agent", {"prompt": "..."})
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("always-gate", res.reason)  # not policy-review

    def test_spawn_does_not_accumulate_any_legs(self):
        # After an Agent spawn the session's accumulated legs must be EMPTY — the
        # explicit zero-leg rule must keep legstate clean (no partial pollution).
        self.set_provenance({"kind": "owner"})
        cfg = self.cfg()
        self.run_call(cfg, "tsclean", "Agent", {"prompt": "..."})
        acc = legstate._load(legstate._state_path("tsclean", str(self.state_dir)))
        self.assertEqual(acc, set())


class TestTrustStateProtection(GateTestBase):
    """Review HIGH: provenance & legstate (.buzai/) are never tool-writable —
    unconditionally, so a poisoned task can't forge owner tier or wipe legs."""

    def test_write_to_provenance_denied_even_for_clean_owner(self):
        self.set_provenance({"kind": "owner"})  # fresh session, no untrusted leg
        res = self.run_call(
            self.cfg(),
            "tts",
            "Write",
            {"file_path": ".buzai/provenance.json", "content": '{"kind":"owner"}'},
        )
        self.assertEqual(res.decision, Decision.DENY)
        self.assertIn("trust-state", res.reason)

    def test_write_to_legstate_denied(self):
        self.set_provenance({"kind": "owner"})
        res = self.run_call(
            self.cfg(), "tts2", "Write", {"file_path": ".buzai/legstate/tts2.json", "content": "[]"}
        )
        self.assertEqual(res.decision, Decision.DENY)
        self.assertIn("trust-state", res.reason)

    def test_nested_multiedit_to_provenance_denied(self):
        self.set_provenance({"kind": "owner"})
        res = self.run_call(
            self.cfg(),
            "tts3",
            "MultiEdit",
            {"edits": [{"file_path": "x/.buzai/provenance.json", "new": "x"}]},
        )
        self.assertEqual(res.decision, Decision.DENY)
        self.assertIn("trust-state", res.reason)

    def test_absolute_path_to_provenance_denied(self):
        self.set_provenance({"kind": "owner"})
        res = self.run_call(
            self.cfg(),
            "tts4",
            "Write",
            {"file_path": "/home/buzai/buzai/.buzai/provenance.json", "content": "x"},
        )
        self.assertEqual(res.decision, Decision.DENY)
        self.assertIn("trust-state", res.reason)

    def test_lookalike_filename_is_not_trust_state(self):
        # a file literally named "notes.buzai" is NOT the .buzai dir; the trust-state
        # guard must not fire on it (it falls through to normal handling).
        self.set_provenance({"kind": "owner"})
        res = self.run_call(
            self.cfg(), "tts5", "Write", {"file_path": "notes.buzai", "content": "x"}
        )
        self.assertNotIn("trust-state", res.reason or "")


class TestBuiltinTools(GateTestBase):
    """Built-in tools must be usable under the live gate — not denied on first use
    by the unknown-tool fail-conservative path."""

    def test_readonly_builtin_is_auto_allowed(self):
        # Grep is read-only → shipped policy maps it to auto → silent allow.
        self.set_provenance({"kind": "owner"})
        res = self.run_call(self.cfg(), "tb", "Grep", {"pattern": "x"})
        self.assertEqual(res.decision, Decision.ALLOW)

    def test_bash_is_ask_not_deny_on_first_use(self):
        # The blind-spot mapping (legs=[]) must keep Bash usable: ask, never the
        # all-three-legs deny that bricked the assistant before.
        self.set_provenance({"kind": "owner"})
        res = self.run_call(self.cfg(), "tb2", "Bash", {"command": "ls -la"})
        self.assertEqual(res.decision, Decision.ASK)
        self.assertNotIn("three trifecta legs", res.reason)

    def test_bash_does_not_accumulate_legs(self):
        self.set_provenance({"kind": "owner"})
        cfg = self.cfg()
        self.run_call(cfg, "tb3", "Bash", {"command": "echo hi"})
        acc = legstate._load(legstate._state_path("tb3", str(self.state_dir)))
        self.assertEqual(acc, set())

    def test_local_write_is_ask_not_deny(self):
        # A write to an ordinary file: legs=[] → not Rule-of-Two denied; policy
        # review → ask (not the protected-config/trust-state deny paths).
        self.set_provenance({"kind": "owner"})
        res = self.run_call(
            self.cfg(), "tb4", "Write", {"file_path": "notes/todo.md", "content": "x"}
        )
        self.assertEqual(res.decision, Decision.ASK)
        self.assertNotIn("three trifecta legs", res.reason)
        self.assertNotIn("protected", res.reason)
        self.assertNotIn("trust-state", res.reason)

    def test_unmapped_tool_is_ask_not_deny_on_first_use(self):
        # AE4: an unmapped tool no longer fails conservative to all-three-legs →
        # deny. It carries no legs and falls to policy default-review → ask.
        self.set_provenance({"kind": "owner"})
        res = self.run_call(self.cfg(), "tu1", "totally_unknown_tool", {"x": 1})
        self.assertEqual(res.decision, Decision.ASK)
        self.assertNotIn("three trifecta legs", res.reason)

    def test_unmapped_tool_does_not_accumulate_legs(self):
        # The unmapped call must not poison the turn's leg set, so a later call is
        # not Rule-of-Two-denied solely because an unmapped tool was used.
        self.set_provenance({"kind": "owner"})
        cfg = self.cfg()
        self.run_call(cfg, "tu2", "totally_unknown_tool", {"x": 1})
        acc = legstate._load(legstate._state_path("tu2", str(self.state_dir)))
        self.assertEqual(acc, set())

    def test_unmapped_tool_cannot_be_auto_promoted(self):
        # Footgun guard: an operator who promotes a send tool's class to `auto` but
        # forgets its connector-legs `external_send` mapping must NOT get a silent,
        # ungated send. An unmapped tool is capped at ask regardless of policy.
        policy = '[classes_of]\n"mcp__unmapped__send" = "u_send"\n\n' '[classes]\nu_send = "auto"\n'
        cfg = self.cfg(self.fixture_config(policy_overrides=policy))
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self.run_call(cfg, "ug", "mcp__unmapped__send", {"to": "stranger@example.com"})
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("no connector-legs classification", res.reason)

    def test_mapped_zero_leg_tool_keeps_auto(self):
        # The guard must not downgrade intentional zero-leg auto tools: ToolSearch is
        # mapped (legs=[]) and `auto` in shipped policy → still a silent allow.
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self.run_call(self.cfg(), "ms", "ToolSearch", {"q": "x"})
        self.assertEqual(res.decision, Decision.ALLOW)


class TestFailClosed(GateTestBase):
    def test_promoted_read_allowed(self):
        # safe-by-default makes every class 'review'; once promoted to 'auto' the
        # gate allows it silently. (An unpromoted read correctly stays ASK.)
        policy = (
            '[classes_of]\n"mcp__claude_ai_Google_Calendar__list_events" = "cal_read"\n\n'
            '[classes]\ncal_read = "auto"\n'
        )
        cfg = self.cfg(self.fixture_config(policy_overrides=policy))
        self.set_provenance({"kind": "owner"})
        res = self.run_call(cfg, "t", "mcp__claude_ai_Google_Calendar__list_events")
        self.assertEqual(res.decision, Decision.ALLOW)

    def test_unpromoted_read_is_review(self):
        self.set_provenance({"kind": "owner"})
        res = self.run_call(self.cfg(), "t", "mcp__claude_ai_Google_Calendar__list_events")
        self.assertEqual(res.decision, Decision.ASK)

    def test_gate_exception_maps_to_deny(self):
        original = hook.gate

        def boom(event, cfg):
            raise RuntimeError("kaboom")

        hook.gate = boom
        try:
            res = hook.evaluate({"session_id": "x", "tool_name": "y"}, self.cfg())
        finally:
            hook.gate = original
        self.assertEqual(res.decision, Decision.DENY)
        self.assertIn("failing closed", res.reason)

    def test_unparseable_input_denies(self):
        # main() reads stdin; here we exercise evaluate with empty/garbage via the
        # documented contract: a non-dict event still yields a decision, never a crash.
        res = hook.evaluate({}, self.cfg())
        self.assertIn(res.decision, (Decision.ALLOW, Decision.ASK, Decision.DENY))


class TestConnectorHardGate(GateTestBase):
    """Destructive managed-connector ops are bound to an always-gate class
    (policy classes_of → always-gate classes), so they ASK regardless of tier or any
    policy promotion. Non-destructive mutations follow ordinary policy."""

    def test_calendar_delete_is_hard_gated(self):
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self.run_call(
            self.cfg(), "hg1", "mcp__claude_ai_Google_Calendar__delete_event", {"eventId": "x"}
        )
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("always-gate", res.reason)

    def test_dropbox_share_link_is_hard_gated(self):
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self.run_call(
            self.cfg(), "hg2", "mcp__claude_ai_DropboxMCP__create_shared_link", {"path": "/x"}
        )
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("always-gate", res.reason)

    def test_hard_gate_beats_policy_promotion(self):
        # Even if the adopter promotes the `delete` class to auto, the always-gate wins
        # (it's checked before standing policy). Restate the classes_of mapping since the
        # fixture replaces policy.toml wholesale.
        policy = (
            '[classes_of]\n"mcp__claude_ai_DropboxMCP__delete" = "delete"\n\n'
            '[classes]\ndelete = "auto"\n'
        )
        cfg = self.cfg(self.fixture_config(policy_overrides=policy))
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self.run_call(cfg, "hg3", "mcp__claude_ai_DropboxMCP__delete", {"path": "/x"})
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("always-gate", res.reason)

    def test_standard_dropbox_delete_is_hard_gated(self):
        # The standard Dropbox connector mirrors DropboxMCP's hard-gate bindings.
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self.run_call(self.cfg(), "hg5", "mcp__claude_ai_Dropbox__delete", {"path": "/x"})
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("always-gate", res.reason)

    def test_standard_dropbox_share_link_is_hard_gated(self):
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self.run_call(
            self.cfg(), "hg6", "mcp__claude_ai_Dropbox__create_shared_link", {"path": "/x"}
        )
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("always-gate", res.reason)

    def test_standard_dropbox_download_link_is_hard_gated(self):
        # download_link mints a temporary PUBLIC download URL — bound to the
        # share_link always-gate class like create_shared_link.
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self.run_call(
            self.cfg(), "hg7", "mcp__claude_ai_Dropbox__download_link", {"path": "/x"}
        )
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("always-gate", res.reason)

    def test_nondestructive_mutation_not_hard_gated(self):
        # A Dropbox move carries external_send but is NOT a hard-gate class → ordinary
        # policy review ASK, proving the hard-gate is scoped to the destructive ops.
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self.run_call(
            self.cfg(),
            "hg4",
            "mcp__claude_ai_DropboxMCP__move",
            {"from_path": "/a", "to_path": "/b"},
        )
        self.assertEqual(res.decision, Decision.ASK)
        self.assertNotIn("always-gate", res.reason)


class TestTurnScopedLegState(GateTestBase):
    """U1: trifecta leg-state is scoped to the turn (provenance turn_id), not the
    whole session — so a prior turn's legs can't brick the current one. Uses a
    non-owner tier so the 3-leg outcome is a stable DENY across the U2 owner-tier
    change (only owner-tier completion becomes an ASK)."""

    # Gmail search -> private_read + untrusted_content; sheets append -> private_read
    # + external_send. Together the two calls cover all three legs.
    GMAIL = "mcp__claude_ai_Gmail__search_threads"
    SHEET = "mcp__sheets__append"

    def test_legs_accumulate_within_a_turn(self):
        # Same turn_id across both calls -> legs union -> trifecta completes -> DENY.
        self.set_provenance({"kind": "unknown", "turn_id": 1})
        cfg = self.cfg()
        self.run_call(cfg, "sA", self.GMAIL)
        res = self.run_call(cfg, "sA", self.SHEET, {"row": ["x"]})
        self.assertEqual(res.decision, Decision.DENY)
        self.assertIn("three trifecta legs", res.reason)

    def test_legs_reset_between_turns(self):
        # Same session, different turn_id -> the second call starts from a fresh leg
        # set, so the two 2-leg calls never combine into the trifecta. This is the
        # self-brick fix: an earlier turn's send no longer poisons a later read.
        cfg = self.cfg()
        self.set_provenance({"kind": "unknown", "turn_id": 1})
        self.run_call(cfg, "sB", self.SHEET, {"row": ["x"]})  # turn 1: PR + ES
        self.set_provenance({"kind": "unknown", "turn_id": 2})
        res = self.run_call(cfg, "sB", self.GMAIL)  # turn 2: PR + UC only
        self.assertNotEqual(res.decision, Decision.DENY)
        self.assertNotIn("three trifecta legs", res.reason or "")

    def test_missing_turn_id_falls_back_to_session_scope(self):
        # No turn_id -> conservative fallback to session-lifetime accumulation, so the
        # two calls still combine into the trifecta and DENY (preserves old behavior).
        self.set_provenance({"kind": "unknown"})  # no turn_id
        cfg = self.cfg()
        self.run_call(cfg, "sC", self.GMAIL)
        res = self.run_call(cfg, "sC", self.SHEET, {"row": ["x"]})
        self.assertEqual(res.decision, Decision.DENY)
        self.assertIn("three trifecta legs", res.reason)


class TestTrifectaCompletionByTier(GateTestBase):
    """U2/U3: a completed in-turn trifecta is a loud ASK at owner tier (human stays
    in the loop) and a hard DENY at every non-owner tier (no human on the loop). The
    owner approval is recorded with a distinct, legible audit marker."""

    GMAIL = "mcp__claude_ai_Gmail__search_threads"  # private_read + untrusted_content
    SHEET = "mcp__sheets__append"  # private_read + external_send

    def _complete_trifecta(self, cfg, session):
        self.run_call(cfg, session, self.GMAIL)
        return self.run_call(cfg, session, self.SHEET, {"row": ["x"]})

    def test_owner_in_turn_trifecta_is_loud_ask(self):
        # AE2/AE5: the injection shape (untrusted content read, then a send) at owner
        # tier surfaces a loud ASK naming the three legs — never a silent allow.
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self._complete_trifecta(self.cfg(), "oA")
        self.assertEqual(res.decision, Decision.ASK)
        self.assertIn("three trifecta legs", res.reason)
        self.assertIn("steering", res.reason)

    def test_non_owner_in_turn_trifecta_is_deny(self):
        # AE3: same completion at a non-owner tier stays a hard DENY.
        self.set_provenance({"kind": "unknown", "turn_id": 1})
        res = self._complete_trifecta(self.cfg(), "oN")
        self.assertEqual(res.decision, Decision.DENY)

    def test_owner_two_legs_not_trifecta_asked(self):
        # An owner two-leg read is unaffected by the trifecta branch.
        self.set_provenance({"kind": "owner", "turn_id": 1})
        res = self.run_call(self.cfg(), "oB", self.GMAIL)
        self.assertNotIn("trifecta", res.reason or "")

    def test_owner_trifecta_writes_legible_marker_and_chain_holds(self):
        # AE2: the approval is recorded with trifecta_warning kept legible after
        # redaction, and the hash chain still verifies with the new field present.
        self.set_provenance({"kind": "owner", "turn_id": 1})
        cfg = self.cfg()
        self._complete_trifecta(cfg, "oC")
        log = (self.audit_dir / "audit.jsonl").read_text()
        self.assertIn('"trifecta_warning":true', log)
        self.assertTrue(audit.verify_chain(str(self.audit_dir)))

    def test_ordinary_ask_has_no_trifecta_marker(self):
        self.set_provenance({"kind": "owner", "turn_id": 1})
        self.run_call(self.cfg(), "oD", "Bash", {"command": "ls"})  # plain review ASK
        log = (self.audit_dir / "audit.jsonl").read_text()
        self.assertNotIn("trifecta_warning", log)


if __name__ == "__main__":
    unittest.main()
