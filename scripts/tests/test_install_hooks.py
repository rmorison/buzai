import json
import tempfile
import unittest
from pathlib import Path

from scripts.install_hooks import install, merge_hooks

# Minimal stand-in for trust/settings.example.json's shape.
EXAMPLE = {
    "_comment": "example",
    "hooks": {
        "UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "$DIR/trust/bin/mark-turn", "timeout": 10}]}
        ],
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": "$DIR/trust/bin/gate", "timeout": 10}],
            }
        ],
    },
}


class TestMergeHooks(unittest.TestCase):
    def test_fresh_adds_both_events(self):
        merged = merge_hooks({}, EXAMPLE)
        self.assertIn("UserPromptSubmit", merged["hooks"])
        self.assertIn("PreToolUse", merged["hooks"])

    def test_preserves_unrelated_keys(self):
        merged = merge_hooks({"model": "opus", "permissions": {"x": 1}}, EXAMPLE)
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["permissions"], {"x": 1})

    def test_idempotent(self):
        once = merge_hooks({}, EXAMPLE)
        twice = merge_hooks(once, EXAMPLE)
        self.assertEqual(once, twice)
        self.assertEqual(len(twice["hooks"]["PreToolUse"]), 1)

    def test_appends_alongside_foreign_pretooluse_hook(self):
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/other"}]}
                ]
            }
        }
        merged = merge_hooks(existing, EXAMPLE)
        cmds = [h["command"] for g in merged["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertIn("/usr/bin/other", cmds)  # foreign hook kept
        self.assertIn("$DIR/trust/bin/gate", cmds)  # buzai gate added
        self.assertEqual(len(merged["hooks"]["PreToolUse"]), 2)

    def test_does_not_mutate_input(self):
        existing = {"hooks": {"PreToolUse": []}}
        merge_hooks(existing, EXAMPLE)
        self.assertEqual(existing["hooks"]["PreToolUse"], [])  # caller's dict untouched

    def test_non_object_settings_raises(self):
        for bad in ([], None, 42, "x"):
            with self.assertRaises(ValueError):
                merge_hooks(bad, EXAMPLE)

    def test_non_list_event_raises(self):
        # a hand-edit that turns an event into an object (not an array) fails loudly
        with self.assertRaises(ValueError):
            merge_hooks({"hooks": {"PreToolUse": {"matcher": "*"}}}, EXAMPLE)

    def test_quote_variant_is_deduped(self):
        # an existing hook spelled with quotes around the same command must not double-wire
        existing = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [{"type": "command", "command": '"$DIR"/trust/bin/gate'}],
                    }
                ]
            }
        }
        merged = merge_hooks(existing, EXAMPLE)
        self.assertEqual(len(merged["hooks"]["PreToolUse"]), 1)  # not appended again


class TestInstall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.example = self.dir / "example.json"
        self.example.write_text(json.dumps(EXAMPLE))
        self.target = self.dir / ".claude" / "settings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_when_absent(self):
        self.assertEqual(install(self.target, self.example), "created")
        data = json.loads(self.target.read_text())
        self.assertIn("PreToolUse", data["hooks"])

    def test_second_run_is_noop(self):
        install(self.target, self.example)
        self.assertEqual(install(self.target, self.example), "noop")

    def test_merges_preserving_existing(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text(json.dumps({"model": "opus"}))
        self.assertEqual(install(self.target, self.example), "merged")
        data = json.loads(self.target.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertIn("UserPromptSubmit", data["hooks"])

    def test_malformed_target_fails_without_overwrite(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("{ this is not json")
        with self.assertRaises(ValueError):
            install(self.target, self.example)
        # original content left untouched — never clobbered
        self.assertEqual(self.target.read_text(), "{ this is not json")


if __name__ == "__main__":
    unittest.main()
