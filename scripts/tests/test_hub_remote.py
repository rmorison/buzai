import io
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.hub_remote import (
    DENIED,
    INDETERMINATE,
    LOCAL_ONLY,
    PRIVATE,
    PUBLIC,
    READABLE,
    UNKNOWN,
    AnonymousEndpoints,
    CacheEntry,
    GitResult,
    anonymous_endpoints,
    anonymous_env,
    authenticated_env,
    authenticated_probe,
    cache_is_usable,
    check,
    classify,
    config_url,
    decide,
    default_cache_path,
    default_git_runner,
    default_host_resolver,
    helper_shape,
    main,
    no_config_injection,
    origin_credential_violations,
    read_cache,
    redact,
    url_credential_problem,
    verify,
    write_cache,
)
from scripts.tests.env_isolation import (
    INJECTED_ENV,
    assert_injection_suppressed,
    child_env,
    plant,
)

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)
SSH_URL = "git@github.com:owner/hubs.git"
HTTPS_URL = "https://github.com/owner/hubs.git"

# Real stderr shapes, so the classifier is pinned against what git actually emits.
PRIVATE_HTTPS = GitResult(
    128, "", "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
)
PRIVATE_SSH = GitResult(
    128,
    "",
    "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
)
PUBLIC_READ = GitResult(0, "e83c516\tHEAD\n", "")
UNREACHABLE = GitResult(
    128, "", "fatal: unable to access 'https://github.com/owner/hubs.git/': Could not resolve host"
)
TIMED_OUT = GitResult(124, "", "timed out after 20s", timed_out=True)
# A generic HTTP error body — a proxy, a captive portal, a load balancer. It contains
# "not found", which used to be enough to read it as proof the repo is private.
CAPTIVE_PORTAL = GitResult(
    128,
    "",
    "fatal: unable to access 'https://github.com/owner/hubs.git/': The requested URL "
    "returned error: <html><head><title>Not Found</title></head><body>The page you "
    "requested was not found on this network.</body></html>",
)


def restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def anon_call(args: list[str]) -> bool:
    """True when this argv is the credential-suppressed probe (its marker is the -c reset)."""
    return "credential.helper=" in args


class ScriptedGit:
    """Fake git runner. Records every call so tests can assert what was (not) run."""

    def __init__(self, *, remote_url=None, remote_name="origin", anon=None, auth=None, helpers=()):
        self.remote_url = remote_url
        self.remote_name = remote_name
        self.anon = anon or {}
        self.auth = auth or PUBLIC_READ
        self.helpers = list(helpers)
        self.calls: list[tuple[list[str], dict, float]] = []

    @property
    def probes(self) -> list[list[str]]:
        return [args for args, _, _ in self.calls if "ls-remote" in args]

    def __call__(self, args, env, timeout):
        args = list(args)
        self.calls.append((args, dict(env), timeout))
        if "config" in args:
            if "remote.origin.url" in args:
                return GitResult(0, "https://github.com/rmorison/buzai.git\n", "")
            if not self.helpers:
                return GitResult(1, "", "")
            return GitResult(0, "".join(f"{h}\n" for h in self.helpers), "")
        if "get-url" in args:
            return GitResult(0, f"{self.remote_url}\n", "")
        if args[-1] == "remote":
            if self.remote_url is None:
                return GitResult(0, "", "")
            return GitResult(0, f"{self.remote_name}\n", "")
        if anon_call(args):
            url = args[args.index("ls-remote") + 1]
            if isinstance(self.anon, GitResult):
                return self.anon
            return self.anon.get(url, PRIVATE_HTTPS)
        return self.auth


class HubTempCase(unittest.TestCase):
    """Everything runs in a tempdir: never the real ~/hubs, never the network."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hub = self.root / "hubs"
        (self.hub / ".git").mkdir(parents=True)
        self.repo = self.root / "checkout"
        self.repo.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def verify(self, git, *, now=NOW, use_cache=True, **kw):
        return verify(self.hub, runner=git, now=now, use_cache=use_cache, **kw)


# --- URL handling ----------------------------------------------------------------


class TestAnonymousUrls(unittest.TestCase):
    def urls(self, url, resolve_host=None):
        return list(anonymous_endpoints(url, resolve_host).urls)

    def test_https_is_probed_as_is(self):
        self.assertEqual(self.urls(HTTPS_URL), [HTTPS_URL])

    def test_embedded_credentials_are_stripped_before_probing(self):
        # probing WITH the token would authenticate, and a private repo would read as public
        self.assertEqual(
            self.urls("https://user:ghp_secret@github.com/owner/hubs.git"), [HTTPS_URL]
        )

    def test_scp_style_ssh_also_probes_the_https_endpoint(self):
        # public readability is exposed over https, never over ssh — probing only ssh
        # would report "private" for every repo on earth
        self.assertEqual(self.urls(SSH_URL), [HTTPS_URL.removesuffix(".git") + ".git", SSH_URL])

    def test_ssh_scheme_url_drops_the_ssh_port(self):
        self.assertEqual(
            self.urls("ssh://git@example.test:2222/owner/hubs.git"),
            ["https://example.test/owner/hubs.git", "ssh://example.test:2222/owner/hubs.git"],
        )

    def test_git_protocol_is_already_anonymous(self):
        self.assertEqual(
            self.urls("git://example.test/owner/hubs.git"),
            ["git://example.test/owner/hubs.git"],
        )

    def test_local_path_has_no_anonymous_endpoint(self):
        self.assertEqual(self.urls("/srv/git/hubs.git"), [])
        self.assertEqual(self.urls("file:///srv/git/hubs.git"), [])
        self.assertEqual(self.urls(""), [])

    def test_a_file_url_is_not_mistaken_for_an_scp_like_remote(self):
        # `file:/srv/hubs` matches the scp-like shape with host "file"; deriving
        # https://file/srv/hubs from it would probe a host that does not exist
        self.assertEqual(anonymous_endpoints("file:/srv/git/hubs.git"), AnonymousEndpoints())


class FakeSshConfig:
    """Stand-in for `ssh -G`: the `Host` -> `HostName` map of a config never read here."""

    def __init__(self, **mapping):
        self.mapping = mapping
        self.asked: list[str] = []

    def __call__(self, host):
        self.asked.append(host)
        return self.mapping.get(host)


# Exactly what `deploy/README.md` has the owner configure: a `Host buzai-hub` block
# selecting the deploy key, and a remote written in git's scp-like syntax against it.
ALIAS_URL = "buzai-hub:owner/hubs.git"
ALIAS_CONFIG = FakeSshConfig(**{"buzai-hub": "github.com"})


class TestSshAliasRemote(unittest.TestCase):
    """PLANT: the remote form the deploy documentation actually produces.

    An alias is not a hostname and `host:path` is not a URL, so this remote used to
    yield no anonymous endpoint at all — INDETERMINATE, which fails closed, on every
    push forever, for an owner who followed the instructions exactly.
    """

    def endpoints(self, url=ALIAS_URL, resolve_host=ALIAS_CONFIG):
        return anonymous_endpoints(url, resolve_host)

    def test_the_documented_remote_yields_an_https_twin(self):
        self.assertEqual(
            list(self.endpoints().urls), ["https://github.com/owner/hubs.git", ALIAS_URL]
        )

    def test_nothing_is_wrong_with_it(self):
        self.assertIsNone(self.endpoints().problem)

    def test_the_alias_is_resolved_rather_than_used_as_a_hostname(self):
        resolver = FakeSshConfig(**{"buzai-hub": "github.com"})
        urls = anonymous_endpoints(ALIAS_URL, resolver).urls
        self.assertEqual(resolver.asked, ["buzai-hub"])
        self.assertFalse([u for u in urls if u.startswith("https://buzai-hub")])

    def test_an_alias_in_an_ssh_scheme_url_is_resolved_too(self):
        self.assertEqual(
            list(self.endpoints("ssh://buzai-hub/owner/hubs.git").urls),
            ["https://github.com/owner/hubs.git", "ssh://buzai-hub/owner/hubs.git"],
        )

    def test_a_real_hostname_still_works_when_ssh_echoes_it_back(self):
        # `ssh -G github.com` with no Host block answers "hostname github.com"
        resolver = FakeSshConfig(**{"github.com": "github.com"})
        self.assertEqual(list(anonymous_endpoints(SSH_URL, resolver).urls), [HTTPS_URL, SSH_URL])

    def test_an_unresolvable_alias_derives_NO_endpoint(self):
        # not "just the ssh URL": an anonymous ssh probe is refused for a PUBLIC repo
        # too, so accepting it alone would manufacture a private verdict
        self.assertEqual(self.endpoints(resolve_host=FakeSshConfig()).urls, ())

    def test_an_unresolvable_alias_says_so_by_name(self):
        problem = self.endpoints(resolve_host=FakeSshConfig()).problem or ""
        self.assertIn("buzai-hub", problem)
        self.assertIn("ssh -G buzai-hub", problem)
        self.assertIn("HostName", problem)


class TestVerifyAnAliasRemote(HubTempCase):
    """The same plant, driven through the whole verdict: setup as documented must push."""

    def probed(self, git) -> list[str]:
        return [args[args.index("ls-remote") + 1] for args in git.probes]

    def test_the_documented_setup_is_verified_private_and_may_push(self):
        git = ScriptedGit(remote_url=ALIAS_URL)
        result = self.verify(git, resolve_host=ALIAS_CONFIG)
        self.assertEqual(result.verdict, PRIVATE)
        self.assertTrue(result.push_allowed)
        self.assertIn("https://github.com/owner/hubs.git", self.probed(git))

    def test_a_public_repo_behind_the_alias_is_still_caught(self):
        # the twin is really probed, not merely derived: this is the refusal that the
        # whole module exists for, and it is unreachable without resolving the alias
        git = ScriptedGit(
            remote_url=ALIAS_URL, anon={"https://github.com/owner/hubs.git": PUBLIC_READ}
        )
        self.assertEqual(self.verify(git, resolve_host=ALIAS_CONFIG).verdict, PUBLIC)

    def test_an_unresolvable_alias_refuses_and_names_itself(self):
        result = self.verify(ScriptedGit(remote_url=ALIAS_URL), resolve_host=FakeSshConfig())
        self.assertEqual(result.verdict, INDETERMINATE)
        self.assertFalse(result.push_allowed)
        self.assertTrue([v for v in result.violations if "buzai-hub" in v], result.violations)

    def test_an_unresolvable_alias_never_probes_the_ssh_endpoint_alone(self):
        git = ScriptedGit(remote_url=ALIAS_URL)
        self.verify(git, resolve_host=FakeSshConfig())
        self.assertEqual([p for p in self.probed(git) if p != "origin"], [])

    def check(self, git, resolve_host):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = check(self.hub, self.repo, runner=git, now=NOW, resolve_host=resolve_host)
        return rc, err.getvalue()

    def test_the_documented_setup_passes_hub_remote_check(self):
        rc, _ = self.check(ScriptedGit(remote_url=ALIAS_URL), ALIAS_CONFIG)
        self.assertEqual(rc, 0)

    def test_an_unresolvable_alias_fails_hub_remote_check_loudly(self):
        # the owner learns at setup time instead of discovering a backlog that never drains
        rc, err = self.check(ScriptedGit(remote_url=ALIAS_URL), FakeSshConfig())
        self.assertEqual(rc, 1)
        self.assertIn("buzai-hub", err)
        self.assertIn("~/.ssh/config", err)


class TestDefaultHostResolver(unittest.TestCase):
    """The real `ssh -G` mechanism, against a stand-in binary.

    Never the real ssh and never the real `~/.ssh/config` — but the subprocess call,
    the parsing and the timeout are the shipped ones.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.argv = self.dir / "argv"

    def fake_ssh(self, body: str) -> str:
        path = self.dir / "ssh"
        path.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {self.argv}\n{body}\n')
        path.chmod(0o755)
        return str(path)

    def test_the_hostname_line_is_the_answer(self):
        ssh = self.fake_ssh("printf 'user git\\nhostname github.com\\nport 22\\n'")
        self.assertEqual(default_host_resolver("buzai-hub", 10.0, ssh), "github.com")
        self.assertEqual(self.argv.read_text().split(), ["-G", "buzai-hub"])

    def test_a_config_ssh_refuses_to_parse_is_not_an_answer(self):
        ssh = self.fake_ssh("echo 'Bad configuration option' >&2; exit 255")
        self.assertIsNone(default_host_resolver("buzai-hub", 10.0, ssh))

    def test_output_without_a_hostname_is_not_an_answer(self):
        self.assertIsNone(default_host_resolver("h", 10.0, self.fake_ssh("printf 'user git\\n'")))

    def test_a_missing_ssh_binary_is_not_an_answer(self):
        self.assertIsNone(default_host_resolver("h", 10.0, str(self.dir / "no-such-ssh")))

    def test_a_hung_ssh_is_killed_rather_than_stalling_every_push(self):
        started = time.monotonic()
        self.assertIsNone(default_host_resolver("h", 0.3, self.fake_ssh("sleep 5")))
        self.assertLess(time.monotonic() - started, 3.0)


class TestUrlCredentialProblem(unittest.TestCase):
    def test_token_in_url_is_a_violation(self):
        problem = url_credential_problem("https://x:ghp_secret@github.com/owner/hubs.git")
        self.assertIsNotNone(problem)
        self.assertNotIn("ghp_secret", problem)  # the refusal must not leak it either

    def test_ssh_urls_are_clean(self):
        self.assertIsNone(url_credential_problem(SSH_URL))
        self.assertIsNone(url_credential_problem("ssh://git@github.com/owner/hubs.git"))

    def test_plain_https_is_clean(self):
        self.assertIsNone(url_credential_problem(HTTPS_URL))


class TestRedact(unittest.TestCase):
    def test_credentials_never_reach_a_printable_url(self):
        self.assertEqual(redact("https://user:ghp_secret@github.com/owner/hubs.git"), HTTPS_URL)

    def test_clean_urls_are_untouched(self):
        self.assertEqual(redact(SSH_URL), SSH_URL)


# --- the anonymity of the anonymous probe ----------------------------------------


class TestAnonymousEnv(unittest.TestCase):
    """If the probe can authenticate, it reports a PUBLIC repo as private — the one
    failure mode that matters. Every suppression below is load-bearing."""

    ENV = anonymous_env("/tmp/neutral")

    def test_agent_and_askpass_are_removed_not_merely_unused(self):
        for name in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS"):
            self.assertIsNone(self.ENV[name], name)
            self.assertIn(name, self.ENV)  # explicitly unset, not just absent

    def test_git_never_prompts(self):
        self.assertEqual(self.ENV["GIT_TERMINAL_PROMPT"], "0")

    def test_user_and_system_config_are_bypassed(self):
        self.assertEqual(self.ENV["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(self.ENV["GIT_CONFIG_SYSTEM"], os.devnull)

    def test_ssh_cannot_offer_a_key(self):
        ssh = self.ENV["GIT_SSH_COMMAND"]
        for opt in ("BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none", "IdentityFile="):
            self.assertIn(opt, ssh)

    def test_repo_local_config_cannot_be_discovered(self):
        self.assertEqual(self.ENV["GIT_CEILING_DIRECTORIES"], "/tmp/neutral")

    def test_config_injection_channels_are_unset(self):
        # these bypass GIT_CONFIG_GLOBAL/SYSTEM entirely: one insteadOf entry here
        # rewrites the probe URL, and one helper entry authenticates it
        for name in ("GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
            self.assertIn(name, self.ENV)
            self.assertIsNone(self.ENV[name], name)

    def test_numbered_config_pairs_are_enumerated_from_the_environment(self):
        # the count is arbitrary, so the names cannot be written down in advance
        env = anonymous_env("/tmp/neutral", INJECTED_ENV)
        for name in ("GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_41"):
            self.assertIn(name, env)
            self.assertIsNone(env[name], name)

    def test_proxy_channels_are_unset(self):
        # a proxy decides who answers for the host, so it decides the verdict
        for name in ("http_proxy", "https_proxy", "all_proxy", "GIT_PROXY_COMMAND"):
            self.assertIsNone(self.ENV[name], name)
            self.assertIsNone(self.ENV[name.upper()], name.upper())


class TestAuthenticatedEnv(unittest.TestCase):
    """Layer 1 applies here too; layer 2 pointedly does not.

    The authenticated probe's SUCCESS is what turns "every anonymous probe was denied"
    into PRIVATE, so an `insteadOf` rewrite aimed at it manufactures the one verdict
    that permits a push. It must be injection-proof — while still being able to
    authenticate, which is why it keeps everything the anonymous probe throws away.
    """

    ENV = authenticated_env(INJECTED_ENV)

    def test_config_injection_and_proxy_channels_are_unset(self):
        for name in ("GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
            self.assertIn(name, self.ENV)
            self.assertIsNone(self.ENV[name], name)
        for name in ("http_proxy", "https_proxy", "all_proxy", "GIT_PROXY_COMMAND"):
            self.assertIsNone(self.ENV[name], name)
            self.assertIsNone(self.ENV[name.upper()], name.upper())

    def test_numbered_config_pairs_are_enumerated_here_as_well(self):
        for name in ("GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_41"):
            self.assertIsNone(self.ENV[name], name)

    def test_the_credential_channels_are_deliberately_left_intact(self):
        # scrubbing these would break the call rather than harden it: the auth probe
        # and the push exist to exercise this instance's deploy key
        for name in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_GLOBAL"):
            self.assertNotIn(name, self.ENV, name)

    def test_it_still_cannot_hang_a_tty_less_service(self):
        self.assertEqual(self.ENV["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("BatchMode=yes", self.ENV["GIT_SSH_COMMAND"])

    def test_the_shared_layer_is_shared_not_reimplemented(self):
        floor = no_config_injection(INJECTED_ENV)
        self.assertTrue(floor)
        for name, value in floor.items():
            self.assertEqual(self.ENV[name], value, name)
            self.assertEqual(anonymous_env("/tmp/neutral", INJECTED_ENV)[name], value, name)


class TestProbeEnvironmentIsolation(unittest.TestCase):
    """PLANT the injection channels in the PARENT and prove none reaches the child.

    Driven through the real `default_git_runner` (with `sys.executable` standing in for
    the git binary), so this covers the actual env-construction mechanism — overlay
    semantics included — rather than a fake runner's idea of it.
    """

    def setUp(self):
        plant(self)

    def test_nothing_injected_reaches_the_anonymous_probes_child(self):
        assert_injection_suppressed(self, anonymous_env(tempfile.gettempdir()))

    def test_nothing_injected_reaches_the_authenticated_probes_child(self):
        # the overlay is taken from the real `authenticated_probe` call, not written
        # out by hand, so a call site that forgot to use it fails here
        recorder = ScriptedGit()
        authenticated_probe(Path("/nonexistent/hub"), "origin", recorder, 5.0)
        self.assertEqual(len(recorder.calls), 1)
        assert_injection_suppressed(self, recorder.calls[0][1])

    def test_the_anonymous_probes_extra_suppressions_still_hold(self):
        child = child_env(self, anonymous_env(tempfile.gettempdir()))
        for name in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS"):
            self.assertNotIn(name, child, name)
        self.assertEqual(child["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(child["GIT_CONFIG_SYSTEM"], os.devnull)
        self.assertEqual(child["GIT_TERMINAL_PROMPT"], "0")


class TestAnonymousProbeArgs(HubTempCase):
    def setUp(self):
        super().setUp()
        git = ScriptedGit(remote_url=HTTPS_URL, anon=PRIVATE_HTTPS)
        self.verify(git, use_cache=False)
        self.anon = [args for args in git.probes if anon_call(args)]
        self.assertTrue(self.anon)

    def config_overrides(self, args) -> list[str]:
        return [args[i + 1] for i, a in enumerate(args) if a == "-c"]

    def test_probe_resets_the_credential_helper_list(self):
        for args in self.anon:
            self.assertEqual(args[args.index("-c") + 1], "credential.helper=")

    def test_probe_resets_the_proxy_on_the_command_line_too(self):
        # command-line -c outranks every config file, so a proxy that reached git
        # through a channel the environment overlay does not know about is still gone
        for args in self.anon:
            self.assertIn("http.proxy=", self.config_overrides(args))


class TestAuthenticatedProbeArgs(unittest.TestCase):
    def setUp(self):
        self.git = ScriptedGit()
        authenticated_probe(Path("/nonexistent/hub"), "origin", self.git, 5.0)
        self.args = self.git.calls[0][0]

    def test_it_resets_the_proxy_like_the_anonymous_probe(self):
        overrides = [self.args[i + 1] for i, a in enumerate(self.args) if a == "-c"]
        self.assertIn("http.proxy=", overrides)

    def test_it_does_not_reset_the_credential_helper(self):
        # it must be able to authenticate — that success is what distinguishes
        # "private" from "unreachable"
        self.assertNotIn("credential.helper=", self.args)


# --- classification and the three-way decision -----------------------------------


class TestClassify(unittest.TestCase):
    def test_success_is_readable(self):
        self.assertEqual(classify(PUBLIC_READ), READABLE)

    def test_auth_required_is_denied(self):
        self.assertEqual(classify(PRIVATE_HTTPS), DENIED)
        self.assertEqual(classify(PRIVATE_SSH), DENIED)
        self.assertEqual(classify(GitResult(128, "", "remote: Repository not found.")), DENIED)
        self.assertEqual(
            classify(
                GitResult(
                    128,
                    "",
                    "fatal: unable to access 'https://x/': The requested URL returned error: 403",
                )
            ),
            DENIED,
        )

    def test_unreachable_is_unknown(self):
        self.assertEqual(classify(UNREACHABLE), UNKNOWN)
        self.assertEqual(classify(GitResult(128, "", "Host key verification failed.")), UNKNOWN)

    def test_an_unknown_host_key_is_unknown_including_gits_trailing_line(self):
        # the full stderr git actually emits. Host-key checking is deliberately not
        # weakened, so this failure must stay indeterminate — never "proof of private"
        self.assertEqual(
            classify(
                GitResult(
                    128,
                    "",
                    "Host key verification failed.\r\n"
                    "fatal: Could not read from remote repository.",
                )
            ),
            UNKNOWN,
        )

    def test_timeout_is_unknown(self):
        self.assertEqual(classify(TIMED_OUT), UNKNOWN)

    def test_unrecognized_failure_is_unknown_not_denied(self):
        # the permissive-default guard: silence must never read as "denied" (=private)
        self.assertEqual(classify(GitResult(1, "", "")), UNKNOWN)

    def test_a_generic_not_found_error_body_is_not_proof_of_privacy(self):
        # PLANT: a captive-portal / proxy error page containing "not found". A bare
        # "not found" pattern read this as DENIED, i.e. as evidence the repo is private
        self.assertEqual(classify(CAPTIVE_PORTAL), UNKNOWN)

    def test_gits_own_repository_not_found_is_still_denied(self):
        self.assertEqual(classify(GitResult(128, "", "remote: Repository not found.")), DENIED)


class TestDecide(unittest.TestCase):
    """The pure core. A permissive default here is the whole privacy story failing."""

    def test_denied_everywhere_plus_authenticated_read_is_private(self):
        verdict, _ = decide([(HTTPS_URL, DENIED), (SSH_URL, DENIED)], READABLE)
        self.assertEqual(verdict, PRIVATE)

    def test_any_anonymous_read_is_public(self):
        verdict, _ = decide([(HTTPS_URL, READABLE), (SSH_URL, DENIED)], READABLE)
        self.assertEqual(verdict, PUBLIC)

    def test_public_wins_even_when_the_authenticated_probe_failed(self):
        verdict, _ = decide([(HTTPS_URL, READABLE)], UNKNOWN)
        self.assertEqual(verdict, PUBLIC)

    def test_unreachable_everywhere_is_indeterminate_not_private(self):
        verdict, _ = decide([(HTTPS_URL, UNKNOWN), (SSH_URL, UNKNOWN)], UNKNOWN)
        self.assertEqual(verdict, INDETERMINATE)

    def test_denied_anonymously_but_authenticated_probe_failed_is_indeterminate(self):
        # cannot tell "private" from "the host is down" — refuse rather than assume
        verdict, _ = decide([(HTTPS_URL, DENIED)], UNKNOWN)
        self.assertEqual(verdict, INDETERMINATE)

    def test_one_inconclusive_anonymous_probe_blocks_a_private_verdict(self):
        verdict, _ = decide([(HTTPS_URL, UNKNOWN), (SSH_URL, DENIED)], READABLE)
        self.assertEqual(verdict, INDETERMINATE)

    def test_no_anonymous_endpoint_is_indeterminate(self):
        verdict, _ = decide([], READABLE)
        self.assertEqual(verdict, INDETERMINATE)


# --- verification end to end (fake runner, no network) ---------------------------


class TestVerifyHappyPath(HubTempCase):
    def setUp(self):
        super().setUp()
        self.git = ScriptedGit(remote_url=SSH_URL, anon=PRIVATE_HTTPS, auth=PUBLIC_READ)
        self.result = self.verify(self.git)

    def test_verified_private_and_push_permitted(self):
        self.assertEqual(self.result.verdict, PRIVATE)
        self.assertTrue(self.result.push_allowed)
        self.assertEqual(self.result.source, "probe")

    def test_both_endpoints_were_probed_anonymously(self):
        anon = [a for a in self.git.probes if anon_call(a)]
        self.assertEqual(len(anon), 2)

    def test_verdict_is_cached(self):
        entry = read_cache(default_cache_path(self.hub))
        self.assertEqual((entry.verdict, entry.url), (PRIVATE, SSH_URL))


class TestVerifyRefusals(HubTempCase):
    def test_public_remote_is_refused_and_named(self):
        git = ScriptedGit(remote_url=HTTPS_URL, anon=PUBLIC_READ, auth=PUBLIC_READ)
        result = self.verify(git)
        self.assertEqual(result.verdict, PUBLIC)
        self.assertFalse(result.push_allowed)
        self.assertIn(HTTPS_URL, result.detail)

    def test_public_verdict_is_never_cached(self):
        git = ScriptedGit(remote_url=HTTPS_URL, anon=PUBLIC_READ)
        self.verify(git)
        self.assertIsNone(read_cache(default_cache_path(self.hub)))

    def test_a_generic_error_body_yields_indeterminate_not_private(self):
        # end to end: an unrecognized failure fails closed (refuse + queue), and in
        # particular never reaches a PRIVATE verdict that would permit a push
        git = ScriptedGit(remote_url=SSH_URL, anon=CAPTIVE_PORTAL, auth=PUBLIC_READ)
        result = self.verify(git)
        self.assertEqual(result.verdict, INDETERMINATE)
        self.assertFalse(result.push_allowed)

    def test_host_unreachable_refuses_rather_than_assuming_private(self):
        git = ScriptedGit(remote_url=SSH_URL, anon=UNREACHABLE, auth=UNREACHABLE)
        result = self.verify(git)
        self.assertEqual(result.verdict, INDETERMINATE)
        self.assertFalse(result.push_allowed)

    def test_no_remote_is_local_only_not_public(self):
        result = self.verify(ScriptedGit(remote_url=None))
        self.assertEqual(result.verdict, LOCAL_ONLY)
        self.assertNotEqual(result.verdict, PUBLIC)
        self.assertFalse(result.push_allowed)

    def test_unreadable_repo_is_indeterminate_not_local_only(self):
        # a failed `git remote` means "not a usable repo", which must not be quietly
        # reported as the benign "configured, but no remote yet"
        def broken(args, env, timeout):
            return GitResult(128, "", "fatal: not a git repository")

        result = self.verify(broken)
        self.assertEqual(result.verdict, INDETERMINATE)
        self.assertFalse(result.push_allowed)

    def test_token_in_the_remote_url_refuses_even_when_private(self):
        git = ScriptedGit(
            remote_url="https://x:ghp_secret@github.com/owner/hubs.git", anon=PRIVATE_HTTPS
        )
        result = self.verify(git)
        self.assertEqual(result.verdict, PRIVATE)
        self.assertTrue(result.violations)
        self.assertFalse(result.push_allowed)
        self.assertNotIn("ghp_secret", result.detail + " ".join(result.violations))


class TestVerifyCacheAndTtl(HubTempCase):
    """The TOCTOU case: a URL-keyed-only cache never notices a flip to public."""

    def setUp(self):
        super().setUp()
        self.git = ScriptedGit(remote_url=SSH_URL, anon=PRIVATE_HTTPS, auth=PUBLIC_READ)
        self.assertEqual(self.verify(self.git).verdict, PRIVATE)

    def test_fresh_cache_short_circuits_the_probes(self):
        later = ScriptedGit(remote_url=SSH_URL, anon=PUBLIC_READ)
        result = self.verify(later, now=NOW + timedelta(minutes=5))
        self.assertEqual(result.source, "cache")
        self.assertEqual([a for a in later.probes if anon_call(a)], [])

    def test_flip_to_public_with_an_unchanged_url_is_caught_after_ttl_expiry(self):
        flipped = ScriptedGit(remote_url=SSH_URL, anon=PUBLIC_READ, auth=PUBLIC_READ)
        result = self.verify(flipped, now=NOW + timedelta(days=2))
        self.assertEqual(result.verdict, PUBLIC)
        self.assertFalse(result.push_allowed)
        self.assertEqual(result.source, "probe")

    def test_expired_cache_that_cannot_be_reverified_refuses(self):
        offline = ScriptedGit(remote_url=SSH_URL, anon=UNREACHABLE, auth=UNREACHABLE)
        result = self.verify(offline, now=NOW + timedelta(days=2))
        self.assertEqual(result.verdict, INDETERMINATE)
        self.assertFalse(result.push_allowed)

    def test_changed_url_ignores_the_cache(self):
        moved = ScriptedGit(remote_url=HTTPS_URL, anon=PUBLIC_READ, auth=PUBLIC_READ)
        result = self.verify(moved, now=NOW + timedelta(minutes=5))
        self.assertEqual(result.verdict, PUBLIC)

    def test_public_verdict_evicts_a_stale_private_entry(self):
        flipped = ScriptedGit(remote_url=SSH_URL, anon=PUBLIC_READ, auth=PUBLIC_READ)
        self.verify(flipped, now=NOW + timedelta(days=2))
        self.assertIsNone(read_cache(default_cache_path(self.hub)))


class TestCacheIsUsable(unittest.TestCase):
    ENTRY = CacheEntry(SSH_URL, PRIVATE, NOW)

    def test_fresh_private_entry_for_the_same_url(self):
        self.assertTrue(cache_is_usable(self.ENTRY, SSH_URL, NOW + timedelta(minutes=1), 3600))

    def test_expired(self):
        self.assertFalse(cache_is_usable(self.ENTRY, SSH_URL, NOW + timedelta(hours=9), 3600))

    def test_different_url(self):
        self.assertFalse(cache_is_usable(self.ENTRY, HTTPS_URL, NOW, 3600))

    def test_missing_entry(self):
        self.assertFalse(cache_is_usable(None, SSH_URL, NOW, 3600))

    def test_non_private_verdict_is_never_reusable(self):
        entry = CacheEntry(SSH_URL, PUBLIC, NOW)
        self.assertFalse(cache_is_usable(entry, SSH_URL, NOW, 3600))

    def test_timestamp_from_the_future_is_not_trusted(self):
        # a clock jump must not extend the window indefinitely
        self.assertFalse(cache_is_usable(self.ENTRY, SSH_URL, NOW - timedelta(days=1), 3600))


class TestCacheIo(HubTempCase):
    def test_round_trip(self):
        path = default_cache_path(self.hub)
        self.assertTrue(write_cache(path, CacheEntry(SSH_URL, PRIVATE, NOW)))
        self.assertEqual(read_cache(path), CacheEntry(SSH_URL, PRIVATE, NOW))

    def test_missing_file_reads_as_no_entry(self):
        self.assertIsNone(read_cache(self.hub / "absent.json"))

    def test_corrupt_cache_reads_as_no_entry_not_as_verified(self):
        path = default_cache_path(self.hub)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertIsNone(read_cache(path))

    def test_naive_timestamp_is_rejected(self):
        path = default_cache_path(self.hub)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"url": SSH_URL, "verdict": PRIVATE, "verified_at": "2026-08-05T12:00:00"})
        )
        self.assertIsNone(read_cache(path))

    def test_never_creates_a_git_directory_for_a_non_repo(self):
        bare = self.root / "not-a-repo"
        bare.mkdir()
        self.assertFalse(write_cache(default_cache_path(bare), CacheEntry(SSH_URL, PRIVATE, NOW)))
        self.assertFalse((bare / ".git").exists())


class TestTwoSessionsWritingTheCacheAtOnce(HubTempCase):
    """PLANT: two sessions verifying the same hub concurrently, interleaved for real.

    `--spawn=same-dir` is what the unit runs, so two Claude sessions in one directory
    sharing one hub is the normal case, not an exotic one. The write is
    write-temp-then-replace; with a single fixed temp name the SECOND writer overwrites
    the FIRST writer's temp file before the first has replaced, so the first's
    `replace()` fails on a file that is no longer there — it reports failure while the
    cache silently holds the *other* session's entry.

    The interleaving is planted by intercepting the first writer's `replace()` exactly
    once and running the second writer to completion inside it. That is the real race,
    made deterministic; nothing is simulated.

    Verified against the unfixed version (`temp = path.with_suffix('.tmp')`): writer A
    returned False and the cache held B's PUBLIC-URL entry instead of A's.
    """

    A = CacheEntry("ssh://a.invalid/a.git", PRIVATE, NOW)
    B = CacheEntry("ssh://b.invalid/b.git", PRIVATE, NOW + timedelta(minutes=1))

    def test_neither_session_loses_its_write_to_the_others_temp_file(self):
        path = default_cache_path(self.hub)
        original = Path.replace
        other: list[bool] = []
        entered: list[bool] = []

        second_pid = os.getpid() + 1

        def replace_once(self_path, target):
            if not entered:
                entered.append(True)  # set BEFORE recursing, or the nested write re-enters
                # The second session runs entirely inside the first one's window, and is a
                # separate PROCESS — patching getpid is what makes these two sessions
                # rather than one function called twice.
                with unittest.mock.patch("os.getpid", return_value=second_pid):
                    other.append(write_cache(path, self.B))
            return original(self_path, target)

        with unittest.mock.patch.object(Path, "replace", replace_once):
            first = write_cache(path, self.A)

        self.assertEqual(other, [True], "the second session's write failed")
        self.assertTrue(first, "the first session's write failed — its temp file was clobbered")
        # observable end state: the last writer to replace() wins, and it is intact
        self.assertEqual(read_cache(path), self.A)

    def test_no_temp_file_is_left_behind(self):
        path = default_cache_path(self.hub)
        write_cache(path, self.A)
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])

    def test_the_temp_name_is_scoped_to_this_process(self):
        # the mechanism, stated once: hub_commit.atomic_write's rule, replicated here
        # because hub_commit imports this module and importing it back is a cycle
        path = default_cache_path(self.hub)
        seen = []
        original = Path.replace

        def capture(self_path, target):
            seen.append(self_path.name)
            return original(self_path, target)

        with unittest.mock.patch.object(Path, "replace", capture):
            write_cache(path, self.A)
        self.assertEqual(seen, [f"{path.name}.buzai-tmp.{os.getpid()}"])


class TestTheCredentialHelperFindingIsCredentialFree(HubTempCase):
    """PLANT: an inline shell credential helper carrying a live token.

    `credential.helper = !f() { echo password=ghp_…; }; f` is a documented git idiom, and
    the service sends stderr to the journal — so a finding that interpolated the helper's
    VALUE would write the token to the logs in plaintext. The check meant to protect the
    public origin would be the thing that leaked a credential.

    Verified against the unfixed version (`{helper!r}` on the raw value): the token
    appeared verbatim in the violation string.
    """

    TOKEN = "ghp_ThisIsAFakeTokenForTestsOnly0123456789"
    INLINE = "!f() { echo password=" + TOKEN + "; }; f"

    def violations(self, helper):
        return origin_credential_violations(self.repo, ScriptedGit(helpers=[helper]), 5)

    def test_the_token_never_reaches_the_message(self):
        problems = self.violations(self.INLINE)
        self.assertEqual(len(problems), 1)
        self.assertNotIn(self.TOKEN, problems[0])
        self.assertNotIn("password=", problems[0])
        self.assertIn("<inline shell helper>", problems[0])

    def test_the_violation_is_still_reported(self):
        # the redaction must not become a way of missing the finding
        problems = self.violations(self.INLINE)
        self.assertIn("credential helper", problems[0])
        self.assertIn("--unset-all credential.helper", problems[0])

    def test_a_named_helper_is_still_named(self):
        self.assertIn("(store)", self.violations("store")[0])

    def test_arguments_after_the_helper_name_are_dropped(self):
        # `/opt/helper --token=…` is a value too, and the first token identifies it
        problems = self.violations(f"/opt/helper --token={self.TOKEN}")
        self.assertNotIn(self.TOKEN, problems[0])
        self.assertIn("(/opt/helper)", problems[0])

    def test_helper_shape_is_pure_and_total(self):
        self.assertEqual(helper_shape("store"), "store")
        self.assertEqual(helper_shape("  osxkeychain  "), "osxkeychain")
        self.assertEqual(helper_shape("!anything at all"), "<inline shell helper>")
        self.assertEqual(helper_shape("   "), "<empty>")


# --- the public origin must stay unable to push ----------------------------------


class TestOriginCredentialViolations(HubTempCase):
    def test_configured_helper_is_reported(self):
        git = ScriptedGit(helpers=["store"])
        problems = origin_credential_violations(self.repo, git, 5)
        self.assertEqual(len(problems), 1)
        self.assertIn("store", problems[0])

    def test_no_helper_is_clean(self):
        self.assertEqual(origin_credential_violations(self.repo, ScriptedGit(), 5), [])

    def test_empty_reset_value_is_not_a_violation(self):
        # `credential.helper=` is git's idiom for clearing the list, not for adding one
        self.assertEqual(origin_credential_violations(self.repo, ScriptedGit(helpers=[""]), 5), [])

    def test_scp_style_origin_is_normalized_for_urlmatch(self):
        # git's own `host:path` syntax is NOT a URL; unnormalized, urlmatch exits 128 and
        # this whole check silently passes on the very repo it guards
        self.assertEqual(
            config_url("git@github.com:rmorison/buzai.git"),
            "ssh://github.com/rmorison/buzai.git",
        )
        self.assertEqual(config_url(HTTPS_URL), HTTPS_URL)

    def test_urlmatch_error_falls_back_instead_of_reporting_clean(self):
        def flaky(args, env, timeout):
            if "remote.origin.url" in args:
                return GitResult(0, "git@github.com:rmorison/buzai.git\n", "")
            if "--get-urlmatch" in args:
                return GitResult(128, "", "fatal: invalid URL scheme name")
            return GitResult(0, "store\n", "")

        problems = origin_credential_violations(self.repo, flaky, 5)
        self.assertEqual(len(problems), 1)
        self.assertIn("store", problems[0])

    def test_an_unevaluable_check_is_reported_not_swallowed(self):
        def broken(args, env, timeout):
            if "remote.origin.url" in args:
                return GitResult(0, "git@github.com:rmorison/buzai.git\n", "")
            return GitResult(128, "", "fatal: not a git repository")

        problems = origin_credential_violations(self.repo, broken, 5)
        self.assertEqual(len(problems), 1)
        self.assertIn("unverified", problems[0])


# --- timeout bounding ------------------------------------------------------------


class TestTimeoutBounding(HubTempCase):
    def test_a_hung_probe_is_killed_rather_than_blocking(self):
        started = time.monotonic()
        result = default_git_runner(
            ["-c", "import time; time.sleep(60)"], {}, 1.0, git_bin=sys.executable
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(classify(result), UNKNOWN)
        self.assertLess(time.monotonic() - started, 20)

    def test_an_unrunnable_git_returns_a_result_rather_than_raising(self):
        result = default_git_runner(["--version"], {}, 5.0, git_bin="buzai-not-a-git-binary")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(classify(result), UNKNOWN)

    def test_every_probe_carries_the_timeout(self):
        git = ScriptedGit(remote_url=SSH_URL, anon=PRIVATE_HTTPS)
        self.verify(git, use_cache=False, timeout=7.0)
        self.assertTrue(git.calls)
        self.assertEqual({timeout for _, _, timeout in git.calls}, {7.0})


# --- the verb --------------------------------------------------------------------


class TestCheck(HubTempCase):
    def run_check(self, git, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = check(self.hub, self.repo, runner=git, now=NOW, **kw)
        return rc, out.getvalue(), err.getvalue()

    def test_private_remote_exits_zero(self):
        git = ScriptedGit(remote_url=SSH_URL, anon=PRIVATE_HTTPS, auth=PUBLIC_READ)
        rc, out, _ = self.run_check(git)
        self.assertEqual(rc, 0)
        self.assertIn("PRIVATE", out)

    def test_public_remote_exits_one_and_names_it(self):
        git = ScriptedGit(remote_url=HTTPS_URL, anon=PUBLIC_READ, auth=PUBLIC_READ)
        rc, _, err = self.run_check(git)
        self.assertEqual(rc, 1)
        self.assertIn("hub-remote FAIL", err)
        self.assertIn(HTTPS_URL, err)

    def test_unreachable_exits_one(self):
        git = ScriptedGit(remote_url=SSH_URL, anon=UNREACHABLE, auth=UNREACHABLE)
        rc, _, err = self.run_check(git)
        self.assertEqual(rc, 1)
        self.assertIn("hub-remote FAIL", err)

    def test_local_only_warns_without_failing(self):
        # a durability condition, not a privacy one — it must not become an outage
        rc, out, _ = self.run_check(ScriptedGit(remote_url=None))
        self.assertEqual(rc, 0)
        self.assertIn("local-only", out)

    def test_public_origin_credential_helper_is_a_violation(self):
        git = ScriptedGit(
            remote_url=SSH_URL, anon=PRIVATE_HTTPS, auth=PUBLIC_READ, helpers=["store"]
        )
        rc, _, err = self.run_check(git)
        self.assertEqual(rc, 1)
        self.assertIn("credential helper", err)


class TestMain(unittest.TestCase):
    """main() targets the REAL hub location, so only the pre-probe refusal may run."""

    def test_refused_hub_path_exits_one_before_probing(self):
        previous = os.environ.get("BUZAI_HUBS_DIR")
        self.addCleanup(
            lambda: (
                os.environ.__setitem__("BUZAI_HUBS_DIR", previous)
                if previous is not None
                else os.environ.pop("BUZAI_HUBS_DIR", None)
            )
        )
        os.environ["BUZAI_HUBS_DIR"] = str(Path(__file__).resolve().parents[2])  # the checkout
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = main([])
        self.assertEqual(rc, 1)
        self.assertIn("hub-remote FAIL", err.getvalue())

    def test_an_uninitialized_hub_warns_and_exits_0(self):
        """This module is the unit's ExecStartPre. It verifies a remote; with no store
        there is no remote to verify, which is the state of every instance between
        `make setup` and `make hub-init` — a note, not a leak.

        `FAIL` was reserved for "personal content is exposed"; printing it at every start
        on a routine state teaches the owner to skim past the one word that must not be
        skimmed. Same split as `secrets_preflight.report`.
        """
        previous = os.environ.get("BUZAI_HUBS_DIR")
        self.addCleanup(
            lambda: (
                os.environ.__setitem__("BUZAI_HUBS_DIR", previous)
                if previous is not None
                else os.environ.pop("BUZAI_HUBS_DIR", None)
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["BUZAI_HUBS_DIR"] = str(Path(tmp) / "hubs")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = main([])
            self.assertEqual(rc, 0)
            self.assertIn("hub-remote WARN", err.getvalue())
            self.assertNotIn("FAIL", err.getvalue())
            self.assertFalse((Path(tmp) / "hubs").exists())


if __name__ == "__main__":
    unittest.main()
