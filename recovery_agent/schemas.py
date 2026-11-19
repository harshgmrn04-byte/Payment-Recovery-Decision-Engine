from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


RecoveryAction = Literal[
    "retry_now",
    "schedule_retry",
    "notify_customer",
    "mark_lost",
    "manual_review",
]


class Transaction(BaseModel):
    id: Optional[int] = None
    source_user_id: int
    source_card_id: int
    transaction_ts: datetime
    amount: Decimal
    use_chip: str
    merchant_name: str
    merchant_city: str
    merchant_state: Optional[str] = None
    merchant_zip: Optional[str] = None
    mcc: str
    errors_raw: str = Field(min_length=1)
    error_types: list[str] = Field(min_length=1)
    recoverable: bool
    recovery_action: str
    is_ambiguous: bool = False
    is_fraud: bool = False


class Classification(BaseModel):
    cause: str
    recoverable: bool
    confidence: float = Field(ge=0.0, le=1.0)
    action: RecoveryAction
    classifier: str
    reason: str
    ambiguous: bool = False


class GatewayResult(BaseModel):
    status: str
    recovered: bool
    recovered_amount: Decimal = Decimal("0.00")
    idempotency_key: str
    message: str


class AgentResult(BaseModel):
    transaction_id: Optional[int]
    idempotency_key: str
    classification: Classification
    gateway: GatewayResult
    recovered: bool
    recovered_amount: Decimal
    wasted_retry: bool
    notes: str


class AgentRunRequest(BaseModel):
    mode: str = Field(default="rules", pattern="^(rules|retry_all|llm)$")
    limit: int = Field(default=100, ge=1, le=5000)
    reset_runs: bool = False


class AgentRunResponse(BaseModel):
    mode: str
    processed: int
    recovered: int
    wasted_retries: int
    recovered_amount: Decimal
    results: list[AgentResult]


class IngestRequest(BaseModel):
    sample_size: int = Field(default=100_000, ge=1, le=250_000)
    seed: int = 42
    if_exists: str = Field(default="replace", pattern="^(replace|append|fail)$")


class Metrics(BaseModel):
    processed: int = 0
    recovered: int = 0
    recovery_rate: float = 0.0
    wasted_retries: int = 0
    false_retry_rate: float = 0.0
    recovered_amount: Decimal = Decimal("0.00")
    average_confidence: float = 0.0


class GatewayRetryRequest(BaseModel):
    transaction: Transaction
    action: RecoveryAction
    idempotency_key: str


class WebhookEvent(BaseModel):
    transaction_id: Optional[int]
    idempotency_key: str
    event_type: str
    payload: dict[str, Any]
