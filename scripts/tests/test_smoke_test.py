import unittest

from scripts.smoke_test import evaluate, overall


class TestSmokeTest(unittest.TestCase):
    def _all_pass(self):
        return {k: True for k in ["SC0", "SC1", "SC2", "SC3", "SC4", "SC5", "SC6", "SC7", "SC8"]}

    def test_all_pass_is_pass(self):
        statuses = evaluate(self._all_pass())
        self.assertEqual(overall(statuses), "PASS")

    def test_sc0_fail_blocks_the_rest(self):
        results = self._all_pass()
        results["SC0"] = False
        statuses = evaluate(results)
        self.assertEqual(statuses["SC0"], "fail")
        self.assertEqual(statuses["SC1"], "blocked")
        self.assertEqual(statuses["SC8"], "blocked")
        self.assertEqual(overall(statuses), "INCOMPLETE")

    def test_one_fail_is_incomplete(self):
        results = self._all_pass()
        results["SC5"] = False
        statuses = evaluate(results)
        self.assertEqual(statuses["SC5"], "fail")
        self.assertEqual(overall(statuses), "INCOMPLETE")

    def test_pending_is_incomplete(self):
        results = {"SC0": True}  # SC1-SC8 not yet attested
        statuses = evaluate(results)
        self.assertEqual(statuses["SC3"], "pending")
        self.assertEqual(overall(statuses), "INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
