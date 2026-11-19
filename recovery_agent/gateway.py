from __future__ import annotations

import hashlib
from decimal import Decimal

from .schemas import GatewayResult, Transaction


ACTION_RECOVERY_MULTIPLIER = {
    "retry_now": Decimal("1.00"),
    "schedule_retry": Decimal("1.00"),
    "notify_customer": Decimal("1.00"),
    "mark_lost": Decimal("0.00"),
    "manual_review": Decimal("0.00"),
}
CENTS = Decimal("0.01")


class MockPaymentGateway:
    """Deterministic mock gateway for repeatable local demos and evals."""

    def execute(self, transaction: Transaction, action: str, idempotency_key: str) -> GatewayResult:
        if action in {"mark_lost", "manual_review"}:
            return GatewayResult(
                status="skipped",
                recovered=False,
                recovered_amount=Decimal("0.00"),
                idempotency_key=idempotency_key,
                message=f"No gateway retry for action={action}.",
            )

        probability = self._success_probability(transaction, action)
        score = self._score(idempotency_key)
        recovered = score <= probability
        recovered_amount = (
            transaction.amount * ACTION_RECOVERY_MULTIPLIER.get(action, Decimal("0.00"))
        ).quantize(CENTS)
        if not recovered:
            recovered_amount = Decimal("0.00")

        return GatewayResult(
            status="succeeded" if recovered else "failed",
            recovered=recovered,
            recovered_amount=recovered_amount,
            idempotency_key=idempotency_key,
            message=f"Mock gateway score={score:.3f}, threshold={probability:.3f}.",
        )

    def _success_probability(self, transaction: Transaction, action: str) -> float:
        errors = set(transaction.error_types)
        if errors & {"Bad Card Number", "Bad Expiration"}:
            return 0.0
        if "Technical Glitch" in errors:
            return 0.72
        if "Insufficient Balance" in errors:
            return 0.32 if action == "schedule_retry" else 0.18
        if errors & {"Bad PIN", "Bad CVV", "Bad Zipcode"}:
            return 0.24 if action == "notify_customer" else 0.08
        return 0.05

    def _score(self, idempotency_key: str) -> float:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) / 0xFFFFFFFF
