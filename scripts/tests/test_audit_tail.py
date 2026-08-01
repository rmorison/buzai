import json
import unittest

from scripts.audit_tail import format_entries


def _line(**entry):
    return json.dumps({"prev": "x", "entry": entry, "hash": "h"})


class TestFormatEntries(unittest.TestCase):
    def test_renders_key_fields(self):
        line = _line(
            ts="2026-06-30T12:00:00+00:00",
            tool="mcp__claude_ai_Gmail__search_threads",
            tier="OWNER",
            decision="allow",
            legs=["private_read", "untrusted_content"],
        )
        out = format_entries([line])
        self.assertEqual(len(out), 1)
        for token in ("OWNER", "allow", "search_threads", "private_read,untrusted_content"):
            self.assertIn(token, out[0])

    def test_respects_n(self):
        lines = [_line(ts=str(i), tool="t", tier="OWNER", decision="allow") for i in range(20)]
        self.assertEqual(len(format_entries(lines, n=5)), 5)

    def test_skips_blank_and_unparseable_lines(self):
        good = _line(ts="1", tool="t", tier="OWNER", decision="ask")
        self.assertEqual(len(format_entries(["", "  ", "{not json", good])), 1)

    def test_missing_fields_do_not_crash(self):
        out = format_entries([json.dumps({"entry": {}})])
        self.assertEqual(len(out), 1)
        self.assertIn("?", out[0])

    def test_bare_entry_without_wrapper(self):
        out = format_entries(
            [json.dumps({"ts": "1", "tool": "t", "tier": "OWNER", "decision": "deny"})]
        )
        self.assertIn("deny", out[0])


if __name__ == "__main__":
    unittest.main()
