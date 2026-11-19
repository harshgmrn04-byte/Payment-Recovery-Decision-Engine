from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .ingest import validate_table_name

from .schemas import AgentResult, Classification, Metrics, Transaction, WebhookEvent


FAILED_TRANSACTIONS_TABLE = "failed_transactions"


class PostgresRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    def connect(self):
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def ensure_runtime_tables(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_runs (
                        id BIGSERIAL PRIMARY KEY,
                        transaction_id BIGINT,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        classifier TEXT NOT NULL,
                        cause TEXT NOT NULL,
                        predicted_recoverable BOOLEAN NOT NULL,
                        confidence NUMERIC(4, 3) NOT NULL,
                        action TEXT NOT NULL,
                        gateway_status TEXT NOT NULL,
                        recovered BOOLEAN NOT NULL,
                        recovered_amount NUMERIC(12, 2) NOT NULL,
                        wasted_retry BOOLEAN NOT NULL,
                        ground_truth_recoverable BOOLEAN,
                        transaction_amount NUMERIC(12, 2),
                        notes TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS webhook_events (
                        id BIGSERIAL PRIMARY KEY,
                        transaction_id BIGINT,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL,
                        payload JSONB NOT NULL,
                        received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_agent_runs_created_at ON agent_runs(created_at)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_agent_runs_transaction_id ON agent_runs(transaction_id)"
                )
            conn.commit()

    def reset_agent_runs(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE agent_runs, webhook_events RESTART IDENTITY")
            conn.commit()

    def fetch_transactions(self, limit: int) -> list[Transaction]:
        validate_table_name(FAILED_TRANSACTIONS_TABLE)
        with self.connect() as conn:
            with conn.cursor() as cur:
                rows = cur.execute(
                    f"""
                    SELECT id, source_user_id, source_card_id, transaction_ts, amount,
                           use_chip, merchant_name, merchant_city, merchant_state,
                           merchant_zip, mcc, errors_raw, error_types, recoverable,
                           recovery_action, is_ambiguous, is_fraud
                    FROM {FAILED_TRANSACTIONS_TABLE}
                    ORDER BY id
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
        return [Transaction(**row) for row in rows]

    def reserve_agent_run(
        self,
        transaction: Transaction,
        idempotency_key: str,
        classification: Classification,
    ) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_runs (
                        transaction_id, idempotency_key, classifier, cause,
                        predicted_recoverable, confidence, action, gateway_status,
                        recovered, recovered_amount, wasted_retry,
                        ground_truth_recoverable, transaction_amount, notes
                    )
                    VALUES (
                        %(transaction_id)s, %(idempotency_key)s, %(classifier)s, %(cause)s,
                        %(predicted_recoverable)s, %(confidence)s, %(action)s, 'reserved',
                        false, 0.00, false,
                        %(ground_truth_recoverable)s, %(transaction_amount)s, 'Gateway execution reserved.'
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    {
                        "transaction_id": transaction.id,
                        "idempotency_key": idempotency_key,
                        "classifier": classification.classifier,
                        "cause": classification.cause,
                        "predicted_recoverable": classification.recoverable,
                        "confidence": Decimal(str(classification.confidence)),
                        "action": classification.action,
                        "ground_truth_recoverable": transaction.recoverable,
                        "transaction_amount": transaction.amount,
                    },
                )
                reserved = cur.rowcount == 1
                if reserved:
                    self._apply_pending_webhook(cur, idempotency_key)
            conn.commit()
        return reserved

    def save_agent_result(self, transaction: Transaction, result: AgentResult) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO agent_runs (
                        transaction_id, idempotency_key, classifier, cause,
                        predicted_recoverable, confidence, action, gateway_status,
                        recovered, recovered_amount, wasted_retry,
                        ground_truth_recoverable, transaction_amount, notes
                    )
                    VALUES (
                        %(transaction_id)s, %(idempotency_key)s, %(classifier)s, %(cause)s,
                        %(predicted_recoverable)s, %(confidence)s, %(action)s, %(gateway_status)s,
                        %(recovered)s, %(recovered_amount)s, %(wasted_retry)s,
                        %(ground_truth_recoverable)s, %(transaction_amount)s, %(notes)s
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        classifier = EXCLUDED.classifier,
                        cause = EXCLUDED.cause,
                        predicted_recoverable = EXCLUDED.predicted_recoverable,
                        confidence = EXCLUDED.confidence,
                        action = EXCLUDED.action,
                        gateway_status = EXCLUDED.gateway_status,
                        recovered = EXCLUDED.recovered,
                        recovered_amount = EXCLUDED.recovered_amount,
                        wasted_retry = EXCLUDED.wasted_retry,
                        ground_truth_recoverable = EXCLUDED.ground_truth_recoverable,
                        transaction_amount = EXCLUDED.transaction_amount,
                        notes = EXCLUDED.notes
                    """,
                    {
                        "transaction_id": transaction.id,
                        "idempotency_key": result.idempotency_key,
                        "classifier": result.classification.classifier,
                        "cause": result.classification.cause,
                        "predicted_recoverable": result.classification.recoverable,
                        "confidence": Decimal(str(result.classification.confidence)),
                        "action": result.classification.action,
                        "gateway_status": result.gateway.status,
                        "recovered": result.recovered,
                        "recovered_amount": result.recovered_amount,
                        "wasted_retry": result.wasted_retry,
                        "ground_truth_recoverable": transaction.recoverable,
                        "transaction_amount": transaction.amount,
                        "notes": result.notes,
                    },
                )
                self._apply_pending_webhook(cur, result.idempotency_key)
            conn.commit()

    def record_webhook(self, event: WebhookEvent) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO webhook_events (
                        transaction_id, idempotency_key, event_type, payload
                    )
                    VALUES (%(transaction_id)s, %(idempotency_key)s, %(event_type)s, %(payload)s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    {
                        "transaction_id": event.transaction_id,
                        "idempotency_key": event.idempotency_key,
                        "event_type": event.event_type,
                        "payload": Jsonb(event.payload),
                    },
                )
                if cur.rowcount == 1:
                    self._apply_pending_webhook(cur, event.idempotency_key)
            conn.commit()

    def fetch_agent_run(self, idempotency_key: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                return cur.execute(
                    """
                    SELECT transaction_id, idempotency_key, classifier, cause,
                           predicted_recoverable, confidence, action, gateway_status,
                           recovered, recovered_amount, wasted_retry,
                           ground_truth_recoverable, transaction_amount, notes
                    FROM agent_runs
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                ).fetchone()

    def _apply_pending_webhook(self, cur, idempotency_key: str) -> None:
        event = cur.execute(
            """
            SELECT event_type, payload
            FROM webhook_events
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchone()
        if not event or event["event_type"] != "payment.recovered":
            return

        try:
            recovered_amount = Decimal(str(event["payload"].get("amount", "0.00")))
        except Exception:
            recovered_amount = Decimal("0.00")

        cur.execute(
            """
            UPDATE agent_runs
            SET gateway_status = 'webhook_recovered',
                recovered = true,
                recovered_amount = GREATEST(recovered_amount, %(recovered_amount)s),
                notes = notes || ' Webhook payment.recovered accepted.'
            WHERE idempotency_key = %(idempotency_key)s
            """,
            {
                "idempotency_key": idempotency_key,
                "recovered_amount": recovered_amount,
            },
        )

    def metrics(self) -> Metrics:
        with self.connect() as conn:
            with conn.cursor() as cur:
                row = cur.execute(
                    """
                    SELECT
                        COUNT(*)::int AS processed,
                        COALESCE(SUM(CASE WHEN recovered THEN 1 ELSE 0 END), 0)::int AS recovered,
                        COALESCE(SUM(CASE WHEN recovered AND ground_truth_recoverable THEN 1 ELSE 0 END), 0)::int AS recovered_soft,
                        COALESCE(SUM(CASE WHEN wasted_retry THEN 1 ELSE 0 END), 0)::int AS wasted_retries,
                        COALESCE(SUM(recovered_amount), 0)::numeric AS recovered_amount,
                        COALESCE(AVG(confidence), 0)::float AS average_confidence,
                        COALESCE(SUM(CASE WHEN ground_truth_recoverable THEN 1 ELSE 0 END), 0)::int AS soft_count,
                        COALESCE(SUM(CASE WHEN ground_truth_recoverable = false THEN 1 ELSE 0 END), 0)::int AS hard_count
                    FROM agent_runs
                    """
                ).fetchone()

        processed = row["processed"]
        soft_count = row["soft_count"]
        hard_count = row["hard_count"]
        return Metrics(
            processed=processed,
            recovered=row["recovered"],
            recovery_rate=(row["recovered_soft"] / soft_count) if soft_count else 0.0,
            wasted_retries=row["wasted_retries"],
            false_retry_rate=(row["wasted_retries"] / hard_count) if hard_count else 0.0,
            recovered_amount=row["recovered_amount"],
            average_confidence=row["average_confidence"],
        )

    def recent_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                return cur.execute(
                    """
                    SELECT transaction_id, classifier, cause, action, gateway_status,
                           recovered, recovered_amount, wasted_retry, created_at
                    FROM agent_runs
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
