"""Shared plant-and-dump helper for the environment-isolation tests.

Four network-facing git calls build their environment from `hub_remote`'s overlays —
the anonymous probe, the authenticated probe, `hub_commit`'s push and `hub_review`'s
notes push — and each one's test file proves the same property: the git-config
injection channels planted in the *parent* process do not reach the child. The plant
and the dump live here so there is one definition of "the violation", and so a channel
added to the suppression list is added to every test at once.

The dump runs through the REAL `default_git_runner`, with `sys.executable` standing in
for the git binary, so what is asserted is the actual env-construction mechanism —
overlay semantics, `None`-means-unset and all — rather than a stand-in for it.

Not named `test_*.py`, so unittest discovery never collects it as a test module.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from collections.abc import Mapping

from scripts.hub_remote import default_git_runner

# What no network-facing call may inherit. Values are inert but shaped like the real
# attack: an `insteadOf` rewrite that would send the call to a host of the attacker's
# choosing, a credential helper injected through the numbered pairs, and proxies that
# would answer for the host. `KEY_41` is there so nothing can pass by assuming the
# count is small.
INJECTED_ENV = {
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "url.https://buzai-probe.invalid/.insteadOf",
    "GIT_CONFIG_VALUE_0": "https://github.com/",
    "GIT_CONFIG_KEY_1": "credential.helper",
    "GIT_CONFIG_VALUE_1": "!f() { echo password=x; }; f",
    "GIT_CONFIG_KEY_41": "url.https://buzai-probe.invalid/.insteadOf",
    "GIT_CONFIG_VALUE_41": "git@github.com:",
    "GIT_CONFIG_PARAMETERS": "'credential.helper=store'",
    "GIT_CONFIG": "/nonexistent/attacker.gitconfig",
    "GIT_PROXY_COMMAND": "/nonexistent/attacker-proxy",
    "http_proxy": "http://buzai-probe.invalid:8080",
    "https_proxy": "http://buzai-probe.invalid:8080",
    "all_proxy": "socks5://buzai-probe.invalid:1080",
    "HTTP_PROXY": "http://buzai-probe.invalid:8080",
    "HTTPS_PROXY": "http://buzai-probe.invalid:8080",
    "ALL_PROXY": "socks5://buzai-probe.invalid:1080",
}

DUMP_ENV = "import json, os; print(json.dumps(dict(os.environ)))"


def _restore(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def plant(case: unittest.TestCase) -> None:
    """PLANT the injection channels in the real parent environment; undone on teardown."""
    for name, value in INJECTED_ENV.items():
        case.addCleanup(_restore, name, os.environ.get(name))
        os.environ[name] = value


def child_env(case: unittest.TestCase, overlay: Mapping[str, str | None]) -> dict[str, str]:
    """The environment a child process actually receives for `overlay`."""
    result = default_git_runner(["-c", DUMP_ENV], overlay, 30.0, git_bin=sys.executable)
    case.assertEqual(result.returncode, 0, result.stderr)
    return dict(json.loads(result.stdout))


def assert_injection_suppressed(case: unittest.TestCase, overlay: Mapping[str, str | None]) -> None:
    """The plant is really in the parent, and none of it reaches the child.

    The first half is not ceremony: without it every assertion below would pass against
    an environment that never carried the violation, which is the dead-no-op shape this
    repo has shipped once already.
    """
    for name, value in INJECTED_ENV.items():
        case.assertEqual(os.environ.get(name), value, f"{name} was never planted")
    child = child_env(case, overlay)
    for name in INJECTED_ENV:
        case.assertNotIn(name, child, name)
    # The overlay suppresses named channels; it is not a scorched-earth empty env.
    case.assertIn("PATH", child)
