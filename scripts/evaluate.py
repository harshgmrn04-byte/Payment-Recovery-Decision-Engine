from __future__ import annotations

import argparse
from decimal import Decimal

from recovery_agent.ingest import DEFAULT_TRANSACTIONS_FILE, reservoir_sample_failed
from recovery_agent.agent import RecoveryAgent, build_classifier, is_wasted_retry
from recovery_agent.config import get_settings
from recovery_agent.schemas import Transaction


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate recovery strategies on IBM failed transactions.")
    parser.add_argument("--sample-size", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--modes",
        default="retry_all,rules",
        help="Comma-separated modes: retry_all,rules,llm.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    csv_path = settings.ibm_archive_dir / DEFAULT_TRANSACTIONS_FILE
    prepared_rows, failed_count = reservoir_sample_failed(csv_path, args.sample_size, args.seed)
    transactions = [
        Transaction(
            id=index + 1,
            source_user_id=row.source_user_id,
            source_card_id=row.source_card_id,
            transaction_ts=row.transaction_ts,
            amount=row.amount,
            use_chip=row.use_chip,
            merchant_name=row.merchant_name,
            merchant_city=row.merchant_city,
            merchant_state=row.merchant_state,
            merchant_zip=row.merchant_zip,
            mcc=row.mcc,
            errors_raw=row.errors_raw,
            error_types=row.error_types,
            recoverable=row.recoverable,
            recovery_action=row.recovery_action,
            is_ambiguous=row.is_ambiguous,
            is_fraud=row.is_fraud,
        )
        for index, row in enumerate(prepared_rows)
    ]

    print(f"Failed rows available: {failed_count:,}")
    print(f"Eval sample: {len(transactions):,}")
    print()
    print(
        "mode,classification_accuracy,recovery_rate,false_retry_rate,"
        "recovered_revenue,wasted_retries,classifiers_used"
    )

    for mode in [part.strip() for part in args.modes.split(",") if part.strip()]:
        classifier = build_classifier(mode, settings)
        agent = RecoveryAgent(classifier)
        results = []
        classifier_counts = {}
        for transaction in transactions:
            result = agent.process(transaction)
            result.wasted_retry = is_wasted_retry(transaction, result)
            results.append(result)
            classifier_counts[result.classification.classifier] = (
                classifier_counts.get(result.classification.classifier, 0) + 1
            )

        soft_count = sum(1 for transaction in transactions if transaction.recoverable)
        hard_count = len(transactions) - soft_count
        correct = sum(
            1
            for transaction, result in zip(transactions, results)
            if transaction.recoverable == result.classification.recoverable
        )
        recovered_soft = sum(
            1
            for transaction, result in zip(transactions, results)
            if transaction.recoverable and result.recovered
        )
        wasted = sum(1 for result in results if result.wasted_retry)
        recovered_revenue = sum(
            (result.recovered_amount for result in results),
            Decimal("0.00"),
        )

        print(
            f"{mode},"
            f"{correct / len(transactions):.3f},"
            f"{(recovered_soft / soft_count) if soft_count else 0:.3f},"
            f"{(wasted / hard_count) if hard_count else 0:.3f},"
            f"{recovered_revenue:.2f},"
            f"{wasted},"
            f"{classifier_counts}"
        )


if __name__ == "__main__":
    main()
