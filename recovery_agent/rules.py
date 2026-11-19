from __future__ import annotations

from .ingest import label_recoverability

from .schemas import Classification, Transaction


CAUSE_PRIORITY = [
    ("Technical Glitch", "gateway_or_bank_technical_glitch"),
    ("Insufficient Balance", "insufficient_funds"),
    ("Bad PIN", "incorrect_pin"),
    ("Bad CVV", "incorrect_cvv"),
    ("Bad Zipcode", "billing_zip_mismatch"),
    ("Bad Expiration", "expired_card"),
    ("Bad Card Number", "invalid_card_number"),
]


def classify_with_rules(transaction: Transaction, classifier_name: str = "rules") -> Classification:
    recoverable, action, ambiguous = label_recoverability(transaction.error_types)
    cause = "unknown_error"
    for error, mapped_cause in CAUSE_PRIORITY:
        if error in transaction.error_types:
            cause = mapped_cause
            break

    confidence = 0.92
    if ambiguous or transaction.is_ambiguous:
        confidence = 0.62
    if cause == "unknown_error":
        confidence = 0.35

    reason = (
        f"Mapped Errors?={transaction.errors_raw!r} to {cause}. "
        f"Recommended action: {action}."
    )
    return Classification(
        cause=cause,
        recoverable=recoverable,
        confidence=confidence,
        action=action,
        classifier=classifier_name,
        reason=reason,
        ambiguous=ambiguous or transaction.is_ambiguous,
    )


def classify_retry_all(transaction: Transaction) -> Classification:
    return Classification(
        cause="retry_all_baseline",
        recoverable=True,
        confidence=0.5,
        action="retry_now",
        classifier="retry_all",
        reason="Baseline strategy retries every failed payment without classifying root cause.",
        ambiguous=transaction.is_ambiguous,
    )
