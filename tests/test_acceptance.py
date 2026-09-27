import unittest

from night_market_foundation.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])
        self.assertEqual(20, result["quiz_score"])
        self.assertEqual(2, result["quiz_effective_events"])
        self.assertTrue(result["quiz_resend_all_replayed"])
        self.assertTrue(result["quiz_settled"])
        self.assertTrue(result["quiz_settle_replayed"])


if __name__ == "__main__":
    unittest.main()
