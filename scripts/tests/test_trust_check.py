import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.trust_check import (
    check,
    commented_preset_hint,
    coverage,
    coverage_diff,
    normalize_server,
    parse_mcp_list,
    parse_problems,
)
from trust.config_loader import DEFAULT_CONFIG_DIR

PASS = lambda: (0, "")  # noqa: E731 — fake self-test runner: green
FAIL = lambda: (1, "boom")  # noqa: E731 — fake self-test runner: red

# Shape of real `claude mcp list` output (server display names, not tool names)
MCP_LIST = """\
Checking MCP server health…

claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected
claude.ai Google Calendar: https://calendarmcp.googleapis.com/mcp/v1 - ✔ Connected
context7: npx -y some-server - ✔ Connected
broken-server: https://x.example/mcp - ✘ Failed to connect
"""


class TestCoverage(unittest.TestCase):
    def test_counts_rules(self):
        cfg = {
            "rule": [
                {"match": "a*", "legs": ["private_read"]},
                {"match": "b*", "legs": ["external_send"]},
            ]
        }
        self.assertEqual(coverage(cfg), (2, []))

    def test_flags_zero_leg_connector_rule(self):
        cfg = {
            "rule": [
                {"match": "mcp__claude_ai_Slack__*", "legs": []},  # half-classified connector
                {"match": "mcp__claude_ai_Gmail__*", "legs": ["private_read"]},
            ]
        }
        n, zero = coverage(cfg)
        self.assertEqual(n, 2)
        self.assertEqual(zero, ["mcp__claude_ai_Slack__*"])

    def test_zero_leg_builtin_not_flagged(self):
        # local builtins are intentionally zero-leg — not noise to surface
        cfg = {"rule": [{"match": "Bash*", "legs": []}, {"match": "Write*", "legs": []}]}
        self.assertEqual(coverage(cfg), (2, []))

    def test_ingests_untrusted_counts_as_a_leg(self):
        cfg = {"rule": [{"match": "m*", "ingests_untrusted": True}]}
        self.assertEqual(coverage(cfg), (1, []))

    def test_malformed_rule_shape_does_not_crash(self):
        # `[rule]` (single table) instead of `[[rule]]` -> rule is a dict, not a list
        self.assertEqual(coverage({"rule": {"match": "mcp__x__*", "legs": []}}), (0, []))


class TestParseProblems(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_dir_has_no_problems(self):
        (self.dir / "policy.toml").write_text('default = "review"\n')
        self.assertEqual(parse_problems(self.dir), [])

    def test_malformed_toml_flagged_by_name(self):
        (self.dir / "connector-legs.toml").write_text("[[rule]\nmatch = ")  # broken
        probs = parse_problems(self.dir)
        self.assertEqual(len(probs), 1)
        self.assertIn("connector-legs.toml", probs[0])


class TestCheck(unittest.TestCase):
    def test_shipped_config_passes_with_green_self_test(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check(DEFAULT_CONFIG_DIR, PASS), 0)

    def test_failing_self_test_propagates(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(check(DEFAULT_CONFIG_DIR, FAIL), 1)

    def test_malformed_config_fails_before_self_test(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "tiers.toml").write_text("= nope")
            # self-test runner that would explode if called — proves we fail first
            boom = lambda: (_ for _ in ()).throw(AssertionError("self-test must not run"))  # noqa: E731
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check(d, boom), 1)

    def test_single_rule_table_fails_cleanly(self):
        # valid TOML but `[rule]` (a table) not `[[rule]]` (a table array) -> clean exit 1,
        # not an AttributeError traceback (the footgun trust-check exists to catch)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "connector-legs.toml").write_text('[rule]\nmatch = "mcp__x__*"\nlegs = []\n')
            boom = lambda: (_ for _ in ()).throw(AssertionError("self-test must not run"))  # noqa: E731
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check(d, boom), 1)


class TestNormalizeServer(unittest.TestCase):
    def test_claude_ai_server(self):
        self.assertEqual(normalize_server("claude.ai Gmail"), "mcp__claude_ai_Gmail__")

    def test_spaces_and_colons(self):
        self.assertEqual(
            normalize_server("plugin:playwright:playwright"), "mcp__plugin_playwright_playwright__"
        )
        self.assertEqual(
            normalize_server("claude.ai Google Calendar"), "mcp__claude_ai_Google_Calendar__"
        )


class TestParseMcpList(unittest.TestCase):
    def test_connected_servers_only(self):
        # header skipped; failed server excluded — no live tools to classify
        self.assertEqual(
            parse_mcp_list(MCP_LIST), ["claude.ai Gmail", "claude.ai Google Calendar", "context7"]
        )

    def test_pasted_tool_prefixes_pass_through(self):
        text = "mcp__claude_ai_Gmail__search_threads\n\nmcp__context7__query-docs\n"
        self.assertEqual(
            parse_mcp_list(text),
            ["mcp__claude_ai_Gmail__search_threads", "mcp__context7__query-docs"],
        )

    def test_empty_input(self):
        self.assertEqual(parse_mcp_list(""), [])


class TestCoverageDiff(unittest.TestCase):
    CFG = {
        "rule": [
            {"match": "mcp__claude_ai_Gmail__*", "legs": ["private_read"]},
            {"match": "mcp__claude_ai_Gmail__send", "legs": ["external_send"]},
            {"match": "mcp__claude_ai_Slack__*", "legs": ["private_read"]},
            {"match": "Bash*", "legs": []},  # non-mcp rule never participates
        ]
    }

    def test_full_coverage_is_clean(self):
        uncovered, dead = coverage_diff(
            ["claude.ai Gmail"], {"rule": self.CFG["rule"][:2] + [self.CFG["rule"][3]]}
        )
        self.assertEqual((uncovered, dead), ([], []))

    def test_live_server_with_no_rule_is_uncovered(self):
        uncovered, _ = coverage_diff(["claude.ai Gmail", "context7"], self.CFG)
        self.assertEqual(uncovered, ["context7"])

    def test_rule_with_no_live_server_is_dead(self):
        _, dead = coverage_diff(["claude.ai Gmail"], self.CFG)
        self.assertEqual(dead, ["mcp__claude_ai_Slack__*"])

    def test_pasted_tool_name_covers_its_server_rule(self):
        # a full tool name (from a pasted /mcp list) matches the wildcard rule's prefix
        uncovered, dead = coverage_diff(
            ["mcp__claude_ai_Gmail__search_threads"], {"rule": [self.CFG["rule"][0]]}
        )
        self.assertEqual((uncovered, dead), ([], []))

    def test_no_live_servers_reports_nothing_dead(self):
        # empty live set = no information — don't declare the whole catalog dead
        self.assertEqual(coverage_diff([], self.CFG), ([], []))

    def test_malformed_rule_shape_does_not_crash(self):
        self.assertEqual(coverage_diff(["x"], {"rule": {"match": "mcp__x__*"}}), ([], []))


class TestCommentedPresetHint(unittest.TestCase):
    TEXT = '# [[rule]]\n# match = "mcp__claude_ai_Slack__*"\n# legs = ["private_read"]\n'

    def test_hint_found_for_commented_block(self):
        self.assertTrue(commented_preset_hint("claude.ai Slack", self.TEXT))

    def test_no_hint_for_unknown_server(self):
        self.assertFalse(commented_preset_hint("claude.ai Notion", self.TEXT))


class TestCheckLive(unittest.TestCase):
    def test_uncovered_live_server_fails(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = check(DEFAULT_CONFIG_DIR, PASS, live_names=["totally-unknown-server"])
        self.assertEqual(rc, 1)

    def test_covered_live_server_passes(self):
        with redirect_stdout(io.StringIO()):
            rc = check(DEFAULT_CONFIG_DIR, PASS, live_names=["claude.ai Gmail"])
        self.assertEqual(rc, 0)

    def test_no_live_source_degrades_to_v1(self):
        with redirect_stdout(io.StringIO()):
            rc = check(DEFAULT_CONFIG_DIR, PASS, live_names=None, live_note="claude not on PATH")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
