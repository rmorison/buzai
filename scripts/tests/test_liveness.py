import unittest
from datetime import datetime

from scripts.liveness import socket_established, last_good_turn_age, is_live

NOW = datetime.fromisoformat("2026-06-23 12:00:00").timestamp()


def ss_estab(pid=42, port="443", proc="claude"):
    return f'ESTAB 0 0 10.0.0.5:54321 93.184.216.34:{port} users:(("{proc}",pid={pid},fd=20))'


class TestSocketEstablished(unittest.TestCase):
    def test_relay_socket_owned_by_pid(self):
        self.assertTrue(socket_established(ss_estab(pid=42), 42))
        self.assertTrue(socket_established(ss_estab(pid=42), "42"))

    def test_socket_owned_by_other_pid_not_established(self):
        self.assertFalse(socket_established(ss_estab(pid=999), 42))

    def test_non_443_port_not_established(self):
        # the remote-control PID holding a non-relay socket (e.g. :8080) is not liveness
        self.assertFalse(socket_established(ss_estab(pid=42, port="8080"), 42))

    def test_no_estab_line(self):
        self.assertFalse(socket_established("LISTEN 0 0 0.0.0.0:22 0.0.0.0:*", 42))

    def test_pid_prefix_not_falsely_matched(self):
        # pid=420 must not satisfy a check for pid=42 (comma disambiguates)
        self.assertFalse(socket_established(ss_estab(pid=420), 42))

    def test_falsy_pid_is_not_live(self):
        self.assertFalse(socket_established(ss_estab(pid=42), 0))
        self.assertFalse(socket_established(ss_estab(pid=42), ""))


class TestLastGoodTurn(unittest.TestCase):
    def test_age(self):
        log = "2026-06-23 11:58:00 turn completed ok"
        self.assertEqual(last_good_turn_age(log, "turn completed", now=NOW), 120.0)

    def test_absent(self):
        self.assertIsNone(last_good_turn_age("nothing here", "turn completed", now=NOW))


class TestIsLive(unittest.TestCase):
    def test_socket_only_is_live_when_idle(self):
        # always-on assistant, connected but idle (no turn in logs) → LIVE
        live, reason = is_live(ss_estab(pid=42), 42)
        self.assertTrue(live)
        self.assertIn("relay socket established", reason)

    def test_socket_plus_recent_turn_live_with_activity(self):
        log = "2026-06-23 11:59:00 turn completed"
        live, reason = is_live(ss_estab(pid=42), 42, log, now=NOW)
        self.assertTrue(live)
        self.assertIn("last activity", reason)

    def test_stale_turn_still_live_but_flagged(self):
        # 2h-old turn does NOT make a connected always-on box NOT-LIVE (idle is fine)
        log = "2026-06-23 10:00:00 turn completed"
        live, reason = is_live(ss_estab(pid=42), 42, log, now=NOW)
        self.assertTrue(live)
        self.assertIn("stale", reason)

    def test_no_socket_not_live(self):
        live, reason = is_live(ss_estab(pid=999), 42, "2026-06-23 11:59:00 turn completed", now=NOW)
        self.assertFalse(live)
        self.assertIn("no established relay socket", reason)


if __name__ == "__main__":
    unittest.main()
