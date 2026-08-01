"""Extract the current Remote Control environment URL from journald output.

The relay environment id rotates on every (re)start, so after a restart you
reconnect to the *new* URL. Claude Code exposes no status command for it, so we
parse the service logs. The URL is owner-equivalent access — this prints only to
a TTY unless `--force`, and you should never pipe it to logs/chat/monitoring.

NOTE: the exact log line is an open question (capture a real sample on your
Claude Code version to finalize URL_RE). Core parsing is in `latest_env_url`.
"""

from __future__ import annotations

import re
import subprocess
import sys

URL_RE = re.compile(r"https://claude\.ai/code\?environment=env_[A-Za-z0-9_-]+")


def latest_env_url(text: str):
    """Return the most recent environment URL in journald text, or None."""
    matches = URL_RE.findall(text or "")
    return matches[-1] if matches else None


def _read_source(argv) -> str:
    # Piped input (e.g. `journalctl ... | env_url.py`) wins. But a non-TTY stdin
    # with no data — systemd's /dev/null, a redirect, `ssh host '...'` without -t —
    # must fall back to querying journald, not silently return "" and report no URL.
    piped = "" if sys.stdin.isatty() else sys.stdin.read()
    if piped.strip():
        return piped
    unit = next((a.split("=", 1)[1] for a in argv if a.startswith("--unit=")), "claude-remote")
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-o", "cat", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return ""
    return out.stdout


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    url = latest_env_url(_read_source(argv))
    if url is None:
        print("no environment URL found in service logs", file=sys.stderr)
        return 2
    if not sys.stdout.isatty() and "--force" not in argv:
        print(
            "refusing to print the owner-equivalent URL to a non-TTY (use --force)", file=sys.stderr
        )
        return 3
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
