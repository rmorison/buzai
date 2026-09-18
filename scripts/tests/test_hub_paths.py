import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from scripts.hub_paths import (
    DEFAULT_SOURCE,
    ENV_VAR,
    REPO_ROOT,
    HubPathError,
    expand,
    hub_dir,
    main,
    refusal,
    resolve,
    source_of,
)

HOME = Path("/home/someone")
REPO = Path("/home/someone/buzai")


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
        # Guards against a naive startswith() check: /home/someone/buzai-hubs is not
        # inside /home/someone/buzai.
        self.assertIsNone(refusal(Path("/home/someone/buzai-hubs"), REPO))


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
