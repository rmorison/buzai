"""U1: tests for the per-turn provenance writer (write_provenance + mark_turn)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from trust import mark_turn
from trust.config_loader import load_config
from trust.model import Tier
from trust.provenance import provenance_path, read_provenance, write_provenance
from trust.tiers import resolve_tier


class TestWriteProvenance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.prov_file = self.root / "provenance.json"
        os.environ["BUZAI_PROVENANCE_FILE"] = str(self.prov_file)

    def tearDown(self):
        os.environ.pop("BUZAI_PROVENANCE_FILE", None)
        os.environ.pop("BUZAI_TURN_KIND", None)
        self._tmp.cleanup()

    def _tiers(self):
        return load_config("tiers")

    def test_round_trips_and_resolves_owner(self):
        write_provenance({"kind": "owner"})
        self.assertEqual(read_provenance(), {"kind": "owner"})
        self.assertEqual(resolve_tier(read_provenance(), self._tiers()), Tier.OWNER)

    def test_creates_parent_dir(self):
        nested = self.root / "sub" / ".buzai" / "provenance.json"
        os.environ["BUZAI_PROVENANCE_FILE"] = str(nested)
        write_provenance({"kind": "owner"})
        self.assertTrue(nested.exists())
        self.assertEqual(read_provenance(), {"kind": "owner"})

    def test_atomic_replace_leaves_no_temp(self):
        write_provenance({"kind": "owner"})
        # the temp sibling must not survive a successful write
        self.assertFalse(self.prov_file.with_suffix(".tmp").exists())

    def test_overwrite_replaces_prior_marker(self):
        write_provenance({"kind": "unknown"})
        write_provenance({"kind": "owner"})
        self.assertEqual(read_provenance(), {"kind": "owner"})

    def test_absent_file_is_fail_closed(self):
        # no write performed → read yields {} → untrusted-external
        self.assertEqual(read_provenance(), {})
        self.assertEqual(resolve_tier(read_provenance(), self._tiers()), Tier.UNTRUSTED_EXTERNAL)

    def test_default_workspace_path_uses_relative_default(self):
        os.environ.pop("BUZAI_PROVENANCE_FILE", None)
        self.assertEqual(provenance_path(self.root), self.root / ".buzai/provenance.json")


class TestMarkTurnEntryPoint(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.prov_file = Path(self._tmp.name) / "provenance.json"
        os.environ["BUZAI_PROVENANCE_FILE"] = str(self.prov_file)

    def tearDown(self):
        os.environ.pop("BUZAI_PROVENANCE_FILE", None)
        os.environ.pop("BUZAI_TURN_KIND", None)
        self._tmp.cleanup()

    def test_default_marks_owner(self):
        self.assertEqual(mark_turn.main(), 0)
        self.assertEqual(json.loads(self.prov_file.read_text()), {"kind": "owner", "turn_id": 1})

    def test_env_override_marks_automation(self):
        os.environ["BUZAI_TURN_KIND"] = "automation"
        self.assertEqual(mark_turn.main(), 0)
        self.assertEqual(
            json.loads(self.prov_file.read_text()), {"kind": "automation", "turn_id": 1}
        )
        self.assertEqual(
            resolve_tier(read_provenance(), load_config("tiers")),
            Tier.CONFIGURED_AUTOMATION,
        )

    def test_turn_id_increments_across_turns(self):
        # Each UserPromptSubmit bumps the counter; a fresh start (no prior file)
        # begins at 1. The gate keys leg-state on this so legs reset per turn.
        self.assertEqual(mark_turn.main(), 0)
        self.assertEqual(read_provenance()["turn_id"], 1)
        self.assertEqual(mark_turn.main(), 0)
        self.assertEqual(read_provenance()["turn_id"], 2)
        self.assertEqual(mark_turn.main(), 0)
        self.assertEqual(read_provenance()["turn_id"], 3)

    def test_write_failure_returns_nonzero(self):
        # a path whose parent is an existing regular file can't be created →
        # OSError → main returns 1 (never raises, never wedges the turn).
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("i am a file, not a dir")
        os.environ["BUZAI_PROVENANCE_FILE"] = str(blocker / "provenance.json")
        self.assertEqual(mark_turn.main(), 1)


if __name__ == "__main__":
    unittest.main()
