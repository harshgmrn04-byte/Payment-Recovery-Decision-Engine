import unittest

from recovery_agent.ingest import label_recoverability, split_errors, validate_table_name


class DataPrepTests(unittest.TestCase):
    def test_split_errors_keeps_combined_error_types(self):
        self.assertEqual(
            split_errors("Bad PIN,Technical Glitch"),
            ["Bad PIN", "Technical Glitch"],
        )

    def test_recoverable_soft_decline_maps_to_scheduled_retry(self):
        recoverable, action, ambiguous = label_recoverability(["Insufficient Balance"])

        self.assertTrue(recoverable)
        self.assertEqual(action, "schedule_retry")
        self.assertFalse(ambiguous)

    def test_hard_decline_maps_to_mark_lost(self):
        recoverable, action, ambiguous = label_recoverability(["Bad Card Number"])

        self.assertFalse(recoverable)
        self.assertEqual(action, "mark_lost")
        self.assertFalse(ambiguous)

    def test_mixed_decline_is_preserved_as_ambiguous(self):
        recoverable, action, ambiguous = label_recoverability(
            ["Bad Card Number", "Technical Glitch"]
        )

        self.assertFalse(recoverable)
        self.assertEqual(action, "manual_review")
        self.assertTrue(ambiguous)

    def test_rejects_unsafe_table_name(self):
        with self.assertRaises(ValueError):
            validate_table_name("failed_transactions; drop table users")


if __name__ == "__main__":
    unittest.main()
