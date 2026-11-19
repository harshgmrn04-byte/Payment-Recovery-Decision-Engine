from __future__ import annotations

import unittest

from pydantic import ValidationError

from recovery_agent.config import Settings
from recovery_agent.llm import LLMClassifier
from tests.test_agent import make_transaction


class LLMTests(unittest.TestCase):
    def test_rejects_unexpected_action(self):
        classifier = LLMClassifier(Settings())

        with self.assertRaises(ValidationError):
            classifier._parse_classification(
                {
                    "cause": "made_up",
                    "recoverable": True,
                    "confidence": 0.9,
                    "action": "refund_customer",
                    "reason": "Bad tool call",
                    "ambiguous": False,
                },
                make_transaction(["Technical Glitch"]),
            )

    def test_rejects_string_boolean(self):
        classifier = LLMClassifier(Settings())

        with self.assertRaises(Exception):
            classifier._parse_classification(
                {
                    "cause": "technical_glitch",
                    "recoverable": "false",
                    "confidence": 0.9,
                    "action": "retry_now",
                    "reason": "Provider returned invalid boolean shape",
                    "ambiguous": False,
                },
                make_transaction(["Technical Glitch"]),
            )


if __name__ == "__main__":
    unittest.main()
