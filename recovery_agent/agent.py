from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Dict, Optional, Protocol, Union

from .gateway import MockPaymentGateway
from .llm import SafeLLMClassifier
from .rules import classify_retry_all, classify_with_rules
from .schemas import AgentResult, Classification, GatewayResult, Transaction


class Classifier(Protocol):
    def classify(self, transaction: Transaction) -> Classification:
        ...


class RuleBasedClassifier:
    def classify(self, transaction: Transaction) -> Classification:
        return classify_with_rules(transaction)


class RetryAllClassifier:
    def classify(self, transaction: Transaction) -> Classification:
        return classify_retry_all(transaction)


def build_classifier(mode: str, settings) -> Classifier:
    if mode == "retry_all":
        return RetryAllClassifier()
    if mode == "llm":
        return SafeLLMClassifier(settings)
    return RuleBasedClassifier()


class RecoveryAgent:
    def __init__(
        self,
        classifier: Classifier,
        gateway: Optional[MockPaymentGateway] = None,
    ):
        self.classifier = classifier
        self.gateway = gateway or MockPaymentGateway()

    def process(self, transaction: Transaction) -> AgentResult:
        classification, idempotency_key = self.plan(transaction)
        return self.execute_plan(transaction, classification, idempotency_key)

    def plan(self, transaction: Transaction) -> tuple[Classification, str]:
        classification = self.classifier.classify(transaction)
        idempotency_key = self._idempotency_key(transaction, classification.action)
        return classification, idempotency_key

    def execute_plan(
        self,
        transaction: Transaction,
        classification: Classification,
        idempotency_key: str,
    ) -> AgentResult:
        gateway_result = self.gateway.execute(
            transaction=transaction,
            action=classification.action,
            idempotency_key=idempotency_key,
        )
        notes = self._notes(transaction, classification, gateway_result)
        return AgentResult(
            transaction_id=transaction.id,
            idempotency_key=idempotency_key,
            classification=classification,
            gateway=gateway_result,
            recovered=gateway_result.recovered,
            recovered_amount=gateway_result.recovered_amount,
            wasted_retry=False,
            notes=notes,
        )

    def _idempotency_key(self, transaction: Transaction, action: str) -> str:
        stable_id = (
            f"{transaction.source_user_id}:{transaction.source_card_id}:"
            f"{transaction.transaction_ts.isoformat()}:{transaction.amount}:"
            f"{transaction.merchant_name}:{transaction.mcc}"
        )
        raw = f"{stable_id}:{action}:{transaction.errors_raw}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _notes(
        self,
        transaction: Transaction,
        classification: Classification,
        gateway_result: GatewayResult,
    ) -> str:
        if gateway_result.recovered:
            return f"Recovered ${gateway_result.recovered_amount:.2f} using {classification.action}."
        if classification.action in {"mark_lost", "manual_review"}:
            return f"No retry attempted for ground_truth_recoverable={transaction.recoverable}."
        return "Recovery attempt did not succeed."


def summarize_results(mode: str, results: list[AgentResult]) -> Dict[str, Union[Decimal, int, str]]:
    return {
        "mode": mode,
        "processed": len(results),
        "recovered": sum(1 for result in results if result.recovered),
        "wasted_retries": sum(1 for result in results if result.wasted_retry),
        "recovered_amount": sum(
            (result.recovered_amount for result in results),
            Decimal("0.00"),
        ),
    }


def is_wasted_retry(transaction: Transaction, result: AgentResult) -> bool:
    return (
        result.classification.action in {"retry_now", "schedule_retry", "notify_customer"}
        and not transaction.recoverable
    )
