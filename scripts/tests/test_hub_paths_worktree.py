"""End to end: the resolver run from a linked worktree refuses the main checkout.

Kept apart from `test_hub_paths.py` and importing nothing from the module, so it can
run against any version of `scripts/hub_paths.py`, including the one before the fix:
it copies the module sitting next to this test directory into a scratch repository
and runs it as a script, from the main checkout and from a worktree nested inside it.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "hub_paths.py"


class TestScriptFromAWorktree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1")
        self.env.pop("BUZAI_HUBS_DIR", None)

        self.main = self.root / "main"
        (self.main / "scripts").mkdir(parents=True)
        shutil.copy(MODULE, self.main / "scripts" / "hub_paths.py")
        self.git("init", "-q", cwd=self.main)
        self.git("add", "scripts/hub_paths.py", cwd=self.main)
        self.git("commit", "-q", "-m", "scratch", cwd=self.main)
        self.wt = self.main / ".claude" / "worktrees" / "w"
        self.git("worktree", "add", "-q", "-b", "w", str(self.wt), cwd=self.main)

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args: str, cwd: Path) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "init.defaultBranch=main",
                *args,
            ],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )

    def run_script(self, checkout: Path, hubs_dir: Path) -> subprocess.CompletedProcess:
        env = dict(self.env, BUZAI_HUBS_DIR=str(hubs_dir))
        return subprocess.run(
            [sys.executable, str(checkout / "scripts" / "hub_paths.py")],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_worktree_refuses_a_hub_inside_the_main_checkout(self):
        # AE1: before the fix this exits 0 and resolves the hub into the main checkout.
        result = self.run_script(self.wt, self.main / "hubs" / "x")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("hub-paths FAIL", result.stderr)
        self.assertIn(f"inside the main checkout ({self.main})", result.stderr)

    def test_main_checkout_accepts_a_hub_outside_it(self):
        # AE2: a normal clone behaves as before.
        hubs = self.home / "hubs"
        result = self.run_script(self.main, hubs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"hubs resolve to {hubs}", result.stdout)


if __name__ == "__main__":
    unittest.main()
