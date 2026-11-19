from __future__ import annotations

import json
import unittest
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient

import recovery_agent.app as app_module
from recovery_agent.schemas import Metrics, Transaction


def make_transaction() -> Transaction:
    return Transaction(
        id=1,
        source_user_id=1,
        source_card_id=2,
        transaction_ts=datetime(2026, 1, 1, 10, 0),
        amount=Decimal("100.00"),
        use_chip="Online Transaction",
        merchant_name="Demo Merchant",
        merchant_city="Mumbai",
        merchant_state="MH",
        merchant_zip="400001",
        mcc="5732",
        errors_raw="Technical Glitch",
        error_types=["Technical Glitch"],
        recoverable=True,
        recovery_action="retry_now",
    )


class FakeRepository:
    def __init__(self) -> None:
        self.transactions = [make_transaction()]
        self.reserved_keys: set[str] = set()
        self.saved_results = []
        self.webhooks = []

    def ensure_runtime_tables(self) -> None:
        return None

    def reset_agent_runs(self) -> None:
        self.reserved_keys.clear()
        self.saved_results.clear()
        self.webhooks.clear()

    def fetch_transactions(self, limit: int) -> list[Transaction]:
        return self.transactions[:limit]

    def reserve_agent_run(self, transaction, idempotency_key, classification) -> bool:
        if idempotency_key in self.reserved_keys:
            return False
        self.reserved_keys.add(idempotency_key)
        return True

    def save_agent_result(self, transaction, result) -> None:
        self.saved_results.append(result)

    def metrics(self) -> Metrics:
        return Metrics(processed=len(self.saved_results))

    def recent_runs(self, limit: int):
        return []

    def record_webhook(self, event) -> None:
        self.webhooks.append(event)


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_repository = app_module.repository
        self.repository = FakeRepository()
        app_module.repository = self.repository
        self.client = TestClient(app_module.app)

    def tearDown(self) -> None:
        app_module.repository = self.original_repository

    def test_health_is_available_without_postgres(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_requests_emit_structured_logs(self):
        with self.assertLogs(app_module.request_logger, level="INFO") as captured:
            response = self.client.get("/health")

        record = json.loads(captured.records[0].getMessage())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(record["event"], "http_request")
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["path"], "/health")
        self.assertEqual(record["status_code"], 200)
        self.assertGreaterEqual(record["duration_ms"], 0)

    def test_agent_run_skips_duplicate_idempotency_key(self):
        first = self.client.post("/agent/run", json={"mode": "rules", "limit": 1})
        second = self.client.post("/agent/run", json={"mode": "rules", "limit": 1})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["processed"], 1)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["processed"], 0)
        self.assertEqual(len(self.repository.saved_results), 1)

    def test_gateway_endpoint_requires_configured_token(self):
        payload = {
            "transaction": make_transaction().model_dump(mode="json"),
            "action": "retry_now",
            "idempotency_key": "test-key",
        }

        rejected = self.client.post("/gateway/retry", json=payload)
        accepted = self.client.post(
            "/gateway/retry",
            json=payload,
            headers={"X-Gateway-Token": app_module.settings.gateway_token},
        )

        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 200)

    def test_webhook_is_accepted_once_by_the_repository(self):
        event = {
            "transaction_id": 1,
            "idempotency_key": "webhook-key",
            "event_type": "payment.recovered",
            "payload": {"amount": "100.00"},
        }
        response = self.client.post("/webhooks/payment", json=event)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(self.repository.webhooks), 1)


if __name__ == "__main__":
    unittest.main()
