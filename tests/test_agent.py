from __future__ import annotations

import unittest
from datetime import datetime
from decimal import Decimal

from recovery_agent.agent import (
    RecoveryAgent,
    RetryAllClassifier,
    RuleBasedClassifier,
    is_wasted_retry,
)
from recovery_agent.gateway import MockPaymentGateway
from recovery_agent.schemas import Transaction


def make_transaction(error_types, recoverable=True, amount="100.00"):
    return Transaction(
        id=123,
        source_user_id=1,
        source_card_id=2,
        transaction_ts=datetime(2026, 1, 1, 10, 0),
        amount=Decimal(amount),
        use_chip="Online Transaction",
        merchant_name="Demo Merchant",
        merchant_city="Mumbai",
        merchant_state="MH",
        merchant_zip="400001",
        mcc="5732",
        errors_raw=",".join(error_types),
        error_types=list(error_types),
        recoverable=recoverable,
        recovery_action="retry_now",
        is_ambiguous=False,
        is_fraud=False,
    )


class AgentTests(unittest.TestCase):
    def test_rule_agent_marks_hard_decline_lost(self):
        transaction = make_transaction(["Bad Card Number"], recoverable=False)
        result = RecoveryAgent(RuleBasedClassifier()).process(transaction)

        self.assertEqual(result.classification.action, "mark_lost")
        self.assertFalse(result.wasted_retry)
        self.assertEqual(result.gateway.status, "skipped")

    def test_retry_all_wastes_retry_on_hard_decline(self):
        transaction = make_transaction(["Bad Card Number"], recoverable=False)
        result = RecoveryAgent(RetryAllClassifier()).process(transaction)

        self.assertEqual(result.classification.action, "retry_now")
        self.assertTrue(is_wasted_retry(transaction, result))

    def test_gateway_is_deterministic(self):
        transaction = make_transaction(["Technical Glitch"])
        gateway = MockPaymentGateway()

        first = gateway.execute(transaction, "retry_now", "same-key")
        second = gateway.execute(transaction, "retry_now", "same-key")

        self.assertEqual(first, second)

    def test_idempotency_key_does_not_depend_on_database_id(self):
        first = make_transaction(["Technical Glitch"])
        second = make_transaction(["Technical Glitch"])
        first.id = 1
        second.id = 999
        agent = RecoveryAgent(RuleBasedClassifier())

        _, first_key = agent.plan(first)
        _, second_key = agent.plan(second)

        self.assertEqual(first_key, second_key)


if __name__ == "__main__":
    unittest.main()
