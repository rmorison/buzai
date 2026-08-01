"""Validate the trust-gate configuration and prove the gate still works.

Turns the "carefully uncomment the connector block and fix the match patterns" setup
step into a checked one. Four things:

  1. Every shipped/​local trust config file is valid TOML (a parse error HARD-FAILS and
     names the file — a half-edited connector-legs.toml is the classic footgun).
  2. A coverage summary of connector-legs rules; a rule that resolves to ZERO legs is
     reported as "gates by ask (unclassified)" — informational, NOT a failure, matching
     the gate's conservative default.
  3. (v2) A live coverage diff against the connected MCP servers: `claude mcp list`
     names each connected server; each maps to a tool-name prefix (`mcp__<Server>__`).
     A connected server with NO matching rule is a HARD FAIL — every one of its calls
     falls to the unknown-tool path and its real legs never apply. An `mcp__` rule
     matching no connected server is a "dead pattern" warning (worse than absence:
     it looks classified but never fires). Degrades gracefully: no `claude` binary /
     command failure → note + skip, exit unaffected. Fallback source: a names file
     (one server or tool name per line — paste from `/mcp`) via --names-file.
  4. The gate self-test (the trust unit + gate suite) still passes.

Server-granularity caveat: `claude mcp list` names servers, not tools, so the diff
proves each connected server is classified — per-tool send-override exhaustiveness
(the STANDING RULE in connector-legs.toml) still needs the `/mcp` cross-check when
enabling a connector.

All the v2 pieces (`normalize_server`, `parse_mcp_list`, `coverage_diff`,
`commented_preset_hint`) are pure/injectable; `check` takes the config dir, a
self-test runner, and an optional live-names list so everything is testable without
the real CLI or real files.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trust.classify import rule_legs  # noqa: E402
from trust.config_loader import DEFAULT_CONFIG_DIR, load_config  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# Config base names the gate reads. Each may also have a gitignored <name>.local.toml.
CONFIG_NAMES = ["connector-legs", "tiers", "policy", "always-gate", "known-recipients"]


def parse_problems(config_dir) -> list[str]:
    """Every present <name>.toml / <name>.local.toml must be valid TOML. Returns a list
    of problems, each naming the offending file. Pure w.r.t. the injected directory."""
    d = Path(config_dir)
    probs = []
    for name in CONFIG_NAMES:
        for fname in (f"{name}.toml", f"{name}.local.toml"):
            p = d / fname
            if not p.exists():
                continue
            try:
                with p.open("rb") as fh:
                    tomllib.load(fh)
            except tomllib.TOMLDecodeError as e:
                probs.append(f"{fname}: invalid TOML ({e})")
    return probs


def coverage(legs_config: dict) -> tuple[int, list[str]]:
    """(rule count, CONNECTOR match patterns that resolve to zero legs). Pure.

    A zero-leg rule isn't an error — it gates by the standing policy's default ask. For
    local builtins (Write, Edit, Bash, …) zero legs is intentional and not worth noise.
    But a *connector* rule (match contains `mcp__`) with zero legs is the real footgun:
    a half-classified connector that silently looks fully classified. Only those are
    surfaced, so the check stays clean on the shipped config and loud on a real gap.

    Defensive: a malformed `rule` shape (not a table array) yields (0, []); `check`
    reports that case explicitly before calling here, so this never raises."""
    rules = legs_config.get("rule", [])
    if not isinstance(rules, list):
        return 0, []
    zero = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        match = r.get("match", "<no match>")
        if not rule_legs(r) and "mcp__" in match:
            zero.append(match)
    return len(rules), zero


# --- v2: live coverage diff -----------------------------------------------------


def normalize_server(name: str) -> str:
    """Server display name -> the tool-name prefix its tools carry.

    'claude.ai Gmail' -> 'mcp__claude_ai_Gmail__'; 'context7' -> 'mcp__context7__';
    'plugin:playwright:playwright' -> 'mcp__plugin_playwright_playwright__'.
    Every non-alphanumeric character becomes '_' (matches observed live tool names)."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9]", "_", name.strip()) + "__"


def parse_mcp_list(text: str) -> list[str]:
    """Server display names out of `claude mcp list` output. Pure.

    Lines look like 'NAME: TARGET - ✔ Connected'; headers/blank lines don't match.
    Only connected servers count — a failed server has no live tools to classify.
    A line that is already a tool/prefix name (starts with 'mcp__', e.g. a pasted
    names file) passes through untouched."""
    names = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("mcp__"):
            names.append(line)
            continue
        m = re.match(r"^(.+?):\s+\S.*\s-\s+(.*)$", line)
        if m and "onnected" in m.group(2) and "not" not in m.group(2).lower():
            names.append(m.group(1).strip())
    return names


def coverage_diff(server_names: list[str], legs_config: dict) -> tuple[list[str], list[str]]:
    """(uncovered live servers, dead mcp__ rule patterns). Pure.

    A server is covered when any `mcp__` rule shares a prefix with it in either
    direction: a wildcard rule 'mcp__claude_ai_Gmail__*' covers server prefix
    'mcp__claude_ai_Gmail__', and so does any exact-tool rule under that prefix.
    A rule is dead when it shares a prefix with NO live server. Entries already in
    tool/prefix form (start with 'mcp__') are used as-is."""
    rules = legs_config.get("rule", [])
    if not isinstance(rules, list):
        return [], []
    # (stripped literal, original match string) — report the ORIGINAL pattern so the
    # "fix or remove it" hint names a rule that actually exists in the config.
    literals = [
        (r["match"].rstrip("*"), r["match"])
        for r in rules
        if isinstance(r, dict) and r.get("match", "").startswith("mcp__")
    ]
    prefixes = {(n if n.startswith("mcp__") else normalize_server(n)): n for n in server_names}

    def covers(lit: str, prefix: str) -> bool:
        return lit.startswith(prefix) or prefix.startswith(lit)

    uncovered = [
        display
        for prefix, display in prefixes.items()
        if not any(covers(lit, prefix) for lit, _ in literals)
    ]
    dead = [
        original
        for lit, original in literals
        if prefixes and not any(covers(lit, prefix) for prefix in prefixes)
    ]
    return uncovered, sorted(set(dead))


def commented_preset_hint(server_name: str, legs_toml_text: str) -> bool:
    """True when the shipped catalog carries a COMMENTED preset block for this server —
    the fix is 'uncomment the block', not 'write rules from scratch'. Pure."""
    token = normalize_server(server_name)[len("mcp__") : -2]  # bare server token
    return bool(re.search(rf'^#\s*match\s*=\s*"mcp__{re.escape(token)}', legs_toml_text, re.M))


def live_server_names() -> tuple[list[str] | None, str]:
    """(connected server names, note). None = no live source (degrade to v1)."""
    try:
        r = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return None, "claude not on PATH — skipping live connector diff"
    except subprocess.TimeoutExpired:
        return None, "claude mcp list timed out — skipping live connector diff"
    if r.returncode != 0:
        return None, "claude mcp list failed — skipping live connector diff"
    return parse_mcp_list(r.stdout), ""


def default_self_test_runner() -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "unittest", "trust.tests.test_units", "trust.tests.test_gate"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return r.returncode, r.stdout + r.stderr


def check(
    config_dir, self_test_runner, live_names: list[str] | None = None, live_note: str = ""
) -> int:
    probs = parse_problems(config_dir)
    if probs:
        for p in probs:
            print(f"trust-check FAIL: {p}", file=sys.stderr)
        return 1

    legs_cfg = load_config("connector-legs", config_dir)
    rules = legs_cfg.get("rule", [])
    if not isinstance(rules, list) or any(not isinstance(r, dict) for r in rules):
        print(
            "trust-check FAIL: connector-legs.toml: 'rule' must be a table array "
            "([[rule]]) — found a single [rule] table or a scalar",
            file=sys.stderr,
        )
        return 1
    n, zero = coverage(legs_cfg)
    print(f"trust-check: {n} connector-legs rules parsed OK")
    for match in zero:
        print(f"  note: rule '{match}' resolves to ZERO legs — gates by ask (unclassified)")

    failed = False
    if live_names is None:
        if live_note:
            print(f"trust-check: {live_note} (v1 checks only)")
    else:
        uncovered, dead = coverage_diff(live_names, legs_cfg)
        legs_path = Path(config_dir) / "connector-legs.toml"
        legs_text = legs_path.read_text() if legs_path.exists() else ""
        for name in uncovered:
            hint = (
                " — the shipped catalog has a commented preset block for it: "
                "uncomment it and fix the match patterns"
                if commented_preset_hint(name, legs_text)
                else " — add rules for it (see connector-legs.toml header)"
            )
            print(
                f"trust-check FAIL: connected server '{name}' matches NO rule — every "
                f"call falls to the unknown-tool path (ask) and its real legs never "
                f"apply{hint}",
                file=sys.stderr,
            )
            failed = True
        for pat in dead:
            print(
                f"  warn: rule '{pat}' matches no connected server — dead pattern "
                f"(looks classified, never fires); fix the name against `/mcp` or remove it"
            )
        if not uncovered and not dead:
            print(f"trust-check: all {len(live_names)} connected servers covered by rules")
        print(
            "  note: server-level check — when enabling a connector, still cross-check its "
            "full `/mcp` tool list for send-override exhaustiveness (see connector-legs.toml)"
        )

    rc, out = self_test_runner()
    if rc != 0:
        print("trust-check FAIL: gate self-test failed:\n" + out, file=sys.stderr)
        return 1
    print("trust-check: gate self-test passed")
    return 1 if failed else 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    live_names: list[str] | None
    if "--no-live" in argv:
        live_names, note = None, ""
    elif "--names-file" in argv:
        try:
            path = argv[argv.index("--names-file") + 1]
        except IndexError:
            print("trust-check: --names-file requires a path", file=sys.stderr)
            return 2
        live_names = parse_mcp_list(Path(path).read_text())
        note = ""
    else:
        live_names, note = live_server_names()
    return check(DEFAULT_CONFIG_DIR, default_self_test_runner, live_names, note)


if __name__ == "__main__":
    raise SystemExit(main())
