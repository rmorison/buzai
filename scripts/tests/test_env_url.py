import unittest

from scripts.env_url import latest_env_url


class TestEnvUrl(unittest.TestCase):
    def test_extracts_url(self):
        log = "boot\nremote control at https://claude.ai/code?environment=env_abc123 ready\n"
        self.assertEqual(latest_env_url(log), "https://claude.ai/code?environment=env_abc123")

    def test_none_when_absent(self):
        self.assertIsNone(latest_env_url("just some logs, no url here"))

    def test_returns_most_recent(self):
        log = (
            "https://claude.ai/code?environment=env_OLD\n"
            "...restart...\n"
            "https://claude.ai/code?environment=env_NEW\n"
        )
        self.assertEqual(latest_env_url(log), "https://claude.ai/code?environment=env_NEW")

    def test_empty_input(self):
        self.assertIsNone(latest_env_url(""))


if __name__ == "__main__":
    unittest.main()
