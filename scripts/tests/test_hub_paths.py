import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts.hub_paths import (
    DEFAULT_SOURCE,
    ENV_VAR,
    REPO_ROOT,
    HubPathError,
    expand,
    hub_dir,
    main,
    main_checkout,
    refusal,
    resolve,
    source_of,
)

HOME = Path("/srv/someone")
REPO = Path("/srv/someone/buzai")
MAIN = Path("/srv/someone/buzai-main")


class TestExpand(unittest.TestCase):
    """Lexical expansion only — no filesystem, injected home."""

    def test_unset_is_the_default_dir(self):
        self.assertEqual(expand(None, HOME), HOME / "hubs")

    def test_empty_string_is_treated_as_unset_not_as_root(self):
        # `Environment=BUZAI_HUBS_DIR=` in a unit file arrives as "" — resolving that as
        # a path would silently mean "/" and put hubs at the filesystem root.
        self.assertEqual(expand("", HOME), HOME / "hubs")
        self.assertEqual(expand("   ", HOME), HOME / "hubs")

    def test_tilde_is_expanded_against_the_injected_home(self):
        self.assertEqual(expand("~/knowledge", HOME), HOME / "knowledge")
        self.assertEqual(expand("~", HOME), HOME)

    def test_absolute_value_passes_through(self):
        self.assertEqual(expand("/srv/hubs", HOME), Path("/srv/hubs"))

    def test_relative_value_resolves_against_home(self):
        self.assertEqual(expand("state/hubs", HOME), HOME / "state" / "hubs")

    def test_relative_value_ignores_the_ambient_cwd(self):
        # Sessions are spawned in an arbitrary working directory; a cwd-relative store
        # would move with the caller and split the knowledge base in two.
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as d:
            try:
                os.chdir(d)
                self.assertEqual(expand("state/hubs", HOME), HOME / "state" / "hubs")
            finally:
                os.chdir(original)

    def test_tilde_user_form_is_rejected_rather_than_mangled(self):
        with self.assertRaises(HubPathError):
            expand("~alice/hubs", HOME)


class TestSourceOf(unittest.TestCase):
    def test_configured_value_names_the_env_var(self):
        self.assertEqual(source_of("/srv/hubs"), ENV_VAR)

    def test_unset_and_empty_report_the_default(self):
        self.assertEqual(source_of(None), DEFAULT_SOURCE)
        self.assertEqual(source_of(""), DEFAULT_SOURCE)


class TestRefusal(unittest.TestCase):
    """The decision, over already-resolved paths — pure, no filesystem."""

    def test_path_outside_the_checkout_is_fine(self):
        self.assertIsNone(refusal(HOME / "hubs", REPO))

    def test_path_inside_the_checkout_is_refused(self):
        problem = refusal(REPO / "hubs", REPO)
        self.assertIsNotNone(problem)
        assert problem is not None  # narrow for the reads below
        self.assertIn("inside the repo checkout", problem)
        self.assertIn(str(REPO / "hubs"), problem)
        self.assertIn(ENV_VAR, problem)

    def test_the_checkout_root_itself_is_refused(self):
        problem = refusal(REPO, REPO)
        self.assertIsNotNone(problem)

    def test_path_containing_the_checkout_is_refused(self):
        # `git init` here would swallow the public checkout into the hub repo.
        problem = refusal(HOME, REPO)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("contains the repo checkout", problem)

    def test_sibling_sharing_a_name_prefix_is_fine(self):
        # Guards against a naive startswith() check: /srv/someone/buzai-hubs is not
        # inside /srv/someone/buzai.
        self.assertIsNone(refusal(Path("/srv/someone/buzai-hubs"), REPO))


class TestRefusalOfTheMainCheckout(unittest.TestCase):
    """From a linked worktree (`REPO`), the main checkout (`MAIN`) is refused too. Pure."""

    def test_path_inside_the_main_checkout_is_refused(self):
        problem = refusal(MAIN / "hubs" / "x", REPO, MAIN)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn(f"inside the main checkout ({MAIN})", problem)
        self.assertIn(ENV_VAR, problem)

    def test_the_main_checkout_root_itself_is_refused(self):
        problem = refusal(MAIN, REPO, MAIN)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn("inside the main checkout", problem)

    def test_path_containing_the_main_checkout_is_refused(self):
        # A worktree outside its main checkout: /srv/other contains neither the
        # worktree nor anything but the main checkout.
        main = Path("/srv/other/buzai")
        problem = refusal(Path("/srv/other"), REPO, main)
        self.assertIsNotNone(problem)
        assert problem is not None
        self.assertIn(f"contains the main checkout ({main})", problem)

    def test_sibling_of_the_main_checkout_sharing_a_name_prefix_is_fine(self):
        self.assertIsNone(refusal(Path("/srv/someone/buzai-main-hubs"), REPO, MAIN))

    def test_without_a_main_checkout_the_answers_are_unchanged(self):
        for path in (HOME / "hubs", REPO / "hubs", REPO, HOME, MAIN / "hubs"):
            with self.subTest(path=path):
                self.assertEqual(refusal(path, REPO, None), refusal(path, REPO))
                self.assertEqual(refusal(path, REPO, REPO), refusal(path, REPO))


def _git(*args: str, cwd: Path, home: Path) -> str:
    """Run git for a fixture: scratch HOME, no system config, no inherited GIT_*."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1")
    result = subprocess.run(
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
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


class ScratchRepoTestCase(unittest.TestCase):
    """A scratch main repository with linked worktrees, all in a temp dir.

    `HOME` points at a scratch home for the whole test, so the module's own git call
    reads no real global config either.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.main = self.root / "main"
        self.main.mkdir()
        self.git("init", "-q", cwd=self.main)
        (self.main / "README").write_text("scratch\n")
        self.git("add", "README", cwd=self.main)
        self.git("commit", "-q", "-m", "scratch", cwd=self.main)

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args: str, cwd: Path) -> str:
        return _git(*args, cwd=cwd, home=self.home)

    def add_worktree(self, path: Path, branch: str) -> Path:
        self.git("worktree", "add", "-q", "-b", branch, str(path), cwd=self.main)
        return path


class TestMainCheckout(ScratchRepoTestCase):
    def test_normal_clone_is_none_and_runs_no_git(self):
        # git is made unavailable: if the lookup ran, it would raise, not return None.
        empty = self.root / "empty-bin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            self.assertIsNone(main_checkout(self.main))

    def test_directory_without_git_is_none(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assertIsNone(main_checkout(plain))

    def test_nested_worktree_finds_the_main_checkout(self):
        wt = self.add_worktree(self.main / ".claude" / "worktrees" / "w", "w")
        self.assertEqual(main_checkout(wt), self.main)

    def test_worktree_outside_the_main_checkout_finds_it_too(self):
        wt = self.add_worktree(self.root / "outside", "outside")
        self.assertEqual(main_checkout(wt), self.main)

    def test_broken_gitdir_is_refused_with_the_cause(self):
        broken = self.root / "broken"
        broken.mkdir()
        (broken / ".git").write_text(f"gitdir: {self.root / 'no-such-gitdir'}\n")
        with self.assertRaises(HubPathError) as ctx:
            main_checkout(broken)
        message = str(ctx.exception)
        self.assertIn("main checkout could not be found", message)
        self.assertIn("git worktree list exited", message)

    def test_git_missing_in_a_worktree_is_refused(self):
        wt = self.add_worktree(self.root / "outside", "outside")
        empty = self.root / "empty-bin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            with self.assertRaises(HubPathError) as ctx:
                main_checkout(wt)
        self.assertIn("git is not installed or not on PATH", str(ctx.exception))

    def test_inherited_git_dir_does_not_redirect_the_lookup(self):
        other = self.root / "other"
        other.mkdir()
        self.git("init", "-q", cwd=other)
        wt = self.add_worktree(self.main / ".claude" / "worktrees" / "w", "w")
        with mock.patch.dict(
            os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}
        ):
            self.assertEqual(main_checkout(wt), self.main)

    def test_main_checkout_reported_through_a_symlink_is_resolved(self):
        # git reports real paths on this host, so no fixture reaches the resolve() on
        # its answer; feed it a symlinked path directly.
        wt = self.add_worktree(self.root / "outside", "outside")
        link = self.root / "main-link"
        link.symlink_to(self.main)
        listing = f"worktree {link}\nHEAD 0\nbranch refs/heads/main\n\n"
        real_run = subprocess.run

        def run(cmd, *args, **kwargs):
            if "worktree" in cmd and "list" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")
            return real_run(cmd, *args, **kwargs)

        with mock.patch("scripts.hub_paths.subprocess.run", side_effect=run):
            self.assertEqual(main_checkout(wt), self.main)

    def test_separate_git_dir_main_is_refused(self):
        # git 2.34 lists such a main repository by its git dir, which keeps no pointer
        # back to the working tree: the main checkout can't be found, so refuse.
        sep = self.root / "sep"
        sep.mkdir()
        self.git("init", "-q", "--separate-git-dir", str(self.root / "sep.git"), cwd=sep)
        (sep / "README").write_text("scratch\n")
        self.git("add", "README", cwd=sep)
        self.git("commit", "-q", "-m", "scratch", cwd=sep)
        wt = sep / ".claude" / "worktrees" / "w"
        self.git("worktree", "add", "-q", "-b", "w", str(wt), cwd=sep)
        # Refused whichever way git answers: on 2.34 the lookup can't be checked, and
        # a git that names the real tree refuses it as inside the main checkout.
        with self.assertRaises(HubPathError) as ctx:
            resolve(str(sep / "hubs" / "x"), self.home, wt)
        self.assertRegex(str(ctx.exception), r"could not be found|inside the main checkout")

    def test_stray_dot_git_inside_a_separate_git_dir_does_not_pass(self):
        # git names the git dir; a `.git` entry that merely exists there must not make
        # it count as the main working tree.
        sep = self.root / "sep"
        sep.mkdir()
        sep_git = self.root / "sep.git"
        self.git("init", "-q", "--separate-git-dir", str(sep_git), cwd=sep)
        (sep / "README").write_text("scratch\n")
        self.git("add", "README", cwd=sep)
        self.git("commit", "-q", "-m", "scratch", cwd=sep)
        (sep_git / ".git").mkdir()
        wt = sep / ".claude" / "worktrees" / "w"
        self.git("worktree", "add", "-q", "-b", "w", str(wt), cwd=sep)
        with self.assertRaises(HubPathError):
            resolve(str(sep / "hubs" / "x"), self.home, wt)

    def test_core_worktree_names_the_main_checkout(self):
        # The git dir sits at store/.git, the tree elsewhere: git lists `store`, and
        # only core.worktree knows the real tree.
        store = self.root / "store"
        tree = self.root / "tree"
        store.mkdir()
        tree.mkdir()
        self.git("init", "-q", cwd=store)
        self.git("config", "core.worktree", str(tree), cwd=store)
        (tree / "README").write_text("scratch\n")
        self.git("add", "README", cwd=store)
        self.git("commit", "-q", "-m", "scratch", cwd=store)
        wt = tree / ".claude" / "worktrees" / "w"
        self.git("worktree", "add", "-q", "-b", "w", str(wt), cwd=store)
        self.assertEqual(main_checkout(wt), tree)
        with self.assertRaises(HubPathError) as ctx:
            resolve(str(tree / "hubs" / "x"), self.home, wt)
        self.assertIn(f"inside the main checkout ({tree})", str(ctx.exception))

    def test_worktree_of_a_submodule_finds_the_submodule_checkout(self):
        sup = self.root / "sup"
        sup.mkdir()
        self.git("init", "-q", cwd=sup)
        self.git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(self.main),
            "sub",
            cwd=sup,
        )
        sub = sup / "sub"
        wt = sub / ".claude" / "worktrees" / "w"
        self.git("worktree", "add", "-q", "-b", "w", str(wt), cwd=sub)
        self.assertEqual(main_checkout(wt), sub)
        self.assertEqual(resolve(None, self.home, wt).path, self.home / "hubs")

    def test_bare_main_repository_is_none(self):
        bare = self.root / "bare.git"
        self.git("clone", "-q", "--bare", str(self.main), str(bare), cwd=self.root)
        wt = self.root / "from-bare"
        self.git("worktree", "add", "-q", str(wt), "main", cwd=bare)
        self.assertIsNone(main_checkout(wt))


class TestResolveFromAWorktree(ScratchRepoTestCase):
    def setUp(self):
        super().setUp()
        self.wt = self.add_worktree(self.main / ".claude" / "worktrees" / "w", "w")

    def test_hub_inside_the_main_checkout_is_refused(self):
        with self.assertRaises(HubPathError) as ctx:
            resolve(str(self.main / "hubs" / "x"), self.home, self.wt)
        self.assertIn(f"inside the main checkout ({self.main})", str(ctx.exception))

    def test_hub_outside_both_checkouts_resolves(self):
        location = resolve(None, self.home, self.wt)
        self.assertEqual(location.path, self.home / "hubs")

    def test_hub_reached_through_a_symlink_into_the_main_checkout_is_refused(self):
        (self.home / "hubs").symlink_to(self.main / "private-hubs")
        with self.assertRaises(HubPathError) as ctx:
            resolve(None, self.home, self.wt)
        self.assertIn("inside the main checkout", str(ctx.exception))

    def test_main_checkout_reached_through_a_symlink_is_refused(self):
        # The worktree is reached through a symlinked path to the main checkout, and
        # the hub path through the real one: the comparison must resolve both sides.
        link = self.root / "main-link"
        link.symlink_to(self.main)
        wt_via_link = link / ".claude" / "worktrees" / "w"
        with self.assertRaises(HubPathError):
            resolve(str(self.main / "hubs" / "x"), self.home, wt_via_link)
        with self.assertRaises(HubPathError) as ctx:
            resolve(str(link / "hubs" / "x"), self.home, self.wt)
        self.assertIn("inside the main checkout", str(ctx.exception))

    def test_failed_lookup_refuses_the_resolution(self):
        empty = self.root / "empty-bin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            with self.assertRaises(HubPathError) as ctx:
                resolve(None, self.home, self.wt)
        self.assertIn("main checkout could not be found", str(ctx.exception))


class TestResolve(unittest.TestCase):
    """Full resolution against a real filesystem, including real symlinks."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.home = root / "home"
        self.repo = root / "home" / "buzai"
        self.elsewhere = root / "elsewhere"
        self.repo.mkdir(parents=True)
        self.elsewhere.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_unset_resolves_to_home_hubs(self):
        location = resolve(None, self.home, self.repo)
        self.assertEqual(location.path, self.home / "hubs")
        self.assertEqual(location.source, DEFAULT_SOURCE)

    def test_custom_value_resolves_with_tilde_expanded(self):
        location = resolve("~/knowledge", self.home, self.repo)
        self.assertEqual(location.path, self.home / "knowledge")
        self.assertEqual(location.source, ENV_VAR)

    def test_missing_directory_still_resolves(self):
        # Every instance is in this state before the hub repo is initialized.
        location = resolve(None, self.home, self.repo)
        self.assertFalse(location.path.exists())

    def test_value_inside_the_checkout_is_refused(self):
        with self.assertRaises(HubPathError) as ctx:
            resolve(str(self.repo / "hubs"), self.home, self.repo)
        self.assertIn("inside the repo checkout", str(ctx.exception))

    def test_symlink_whose_target_is_inside_the_checkout_is_refused(self):
        # The case a lexical comparison waves through: the configured path is outside
        # the checkout, but what it actually points at is not.
        target = self.repo / "private-hubs"
        target.mkdir()
        link = self.home / "hubs"
        link.symlink_to(target)
        with self.assertRaises(HubPathError) as ctx:
            resolve(None, self.home, self.repo)
        self.assertIn("inside the repo checkout", str(ctx.exception))

    def test_symlinked_parent_component_is_refused(self):
        # The symlink is an interior component, not the leaf — resolution must follow
        # the whole path, not just its last element.
        link = self.home / "link-to-repo"
        link.symlink_to(self.repo)
        with self.assertRaises(HubPathError):
            resolve(str(link / "hubs"), self.home, self.repo)

    def test_symlink_to_a_safe_target_resolves_to_that_target(self):
        link = self.home / "hubs"
        link.symlink_to(self.elsewhere)
        self.assertEqual(resolve(None, self.home, self.repo).path, self.elsewhere)

    def test_ancestor_of_the_checkout_is_refused(self):
        with self.assertRaises(HubPathError) as ctx:
            resolve(str(self.home), self.home, self.repo)
        self.assertIn("contains the repo checkout", str(ctx.exception))

    def test_location_renders_path_and_source_for_logging(self):
        location = resolve(None, self.home, self.repo)
        rendered = str(location)
        self.assertIn(str(self.home / "hubs"), rendered)
        self.assertIn(DEFAULT_SOURCE, rendered)


class EnvVarTestCase(unittest.TestCase):
    """Save/restore BUZAI_HUBS_DIR around tests that exercise the environment edge."""

    def setUp(self):
        self._saved = os.environ.get(ENV_VAR)
        os.environ.pop(ENV_VAR, None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(ENV_VAR, None)
        else:
            os.environ[ENV_VAR] = self._saved


class TestHubDir(EnvVarTestCase):
    def test_unset_env_uses_home_hubs(self):
        # `.resolve()` on the expectation too: the real home may itself be reached
        # through a symlink, and the resolver deliberately follows those.
        self.assertEqual(hub_dir().path, (Path.home() / "hubs").resolve())

    def test_env_value_wins(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ[ENV_VAR] = d
            location = hub_dir()
            self.assertEqual(location.path, Path(d).resolve())
            self.assertEqual(location.source, ENV_VAR)

    def test_this_repos_hubs_dir_is_refused(self):
        # The violation planted against the real checkout: `hubs/` here holds scaffolds
        # only, and pointing the store at it is exactly the mistake to catch.
        os.environ[ENV_VAR] = str(REPO_ROOT / "hubs")
        with self.assertRaises(HubPathError):
            hub_dir()


class TestMain(EnvVarTestCase):
    def test_reports_the_resolved_path_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ[ENV_VAR] = d
            out = io.StringIO()
            with redirect_stdout(out):
                rc = main([])
            self.assertEqual(rc, 0)
            self.assertIn(str(Path(d).resolve()), out.getvalue())
            self.assertIn(ENV_VAR, out.getvalue())

    def test_missing_directory_is_noted_not_failed(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ[ENV_VAR] = str(Path(d) / "not-yet")
            out = io.StringIO()
            with redirect_stdout(out):
                rc = main([])
            self.assertEqual(rc, 0)
            self.assertIn("does not exist yet", out.getvalue())

    def test_in_checkout_path_fails_loudly_on_stderr(self):
        os.environ[ENV_VAR] = str(REPO_ROOT / "hubs")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = main([])
        self.assertEqual(rc, 1)
        self.assertIn("hub-paths FAIL", err.getvalue())
        self.assertIn("inside the repo checkout", err.getvalue())


if __name__ == "__main__":
    unittest.main()
