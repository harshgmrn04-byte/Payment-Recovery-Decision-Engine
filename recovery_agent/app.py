from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Union

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent import RecoveryAgent, build_classifier, is_wasted_retry, summarize_results
from .config import get_settings
from .db import PostgresRepository
from .gateway import MockPaymentGateway
from .ingest import (
    DEFAULT_TABLE,
    DEFAULT_TRANSACTIONS_FILE,
    find_transactions_csv,
    reservoir_sample_failed,
    write_to_postgres,
)
from .schemas import (
    AgentRunRequest,
    AgentRunResponse,
    GatewayResult,
    GatewayRetryRequest,
    IngestRequest,
    Metrics,
    WebhookEvent,
)


settings = get_settings()
repository = PostgresRepository(settings.database_url)
gateway = MockPaymentGateway()
request_logger = logging.getLogger("payment_recovery.http")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(
    title="Payment Recovery Decision Engine",
    version="1.0.0",
    description=(
        "Classifies failed card payments, chooses a recovery workflow, and records "
        "idempotent recovery outcomes."
    ),
)


@app.middleware("http")
async def log_http_request(request: Request, call_next):
    """Emit one structured record for every completed HTTP request."""
    started_at = time.perf_counter()
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        request_logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                },
                separators=(",", ":"),
            )
        )

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    raise HTTPException(status_code=404, detail="Dashboard index.html not found.")


def database_error(action: str, exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            f"Unable to {action}: {exc}"
        ),
    )


def verify_gateway_token(
    x_gateway_token: Annotated[Optional[str], Header()] = None,
) -> None:
    if x_gateway_token != settings.gateway_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid X-Gateway-Token header is required.",
        )


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness endpoint that does not require a database connection."""
    return {"status": "ok"}


@app.post("/ingest")
def ingest_failed_transactions(request: IngestRequest) -> Dict[str, Union[int, str]]:
    """Sample failed IBM transactions and replace/append the local Postgres table."""
    csv_path = find_transactions_csv(settings.ibm_archive_dir, DEFAULT_TRANSACTIONS_FILE)
    if not csv_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transactions CSV not found: {csv_path}",
        )

    try:
        rows, failed_count = reservoir_sample_failed(csv_path, request.sample_size, request.seed)
        write_to_postgres(rows, settings.database_url, DEFAULT_TABLE, request.if_exists)
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error("ingest failed transactions", exc) from exc

    return {
        "table": DEFAULT_TABLE,
        "failed_rows_available": failed_count,
        "rows_written": len(rows),
    }


@app.post("/agent/run", response_model=AgentRunResponse)
def run_agent(request: AgentRunRequest) -> AgentRunResponse:
    """Process new transactions only; duplicate idempotency keys are never retried."""
    try:
        repository.ensure_runtime_tables()
        if request.reset_runs:
            repository.reset_agent_runs()

        transactions = repository.fetch_transactions(request.limit)
        agent = RecoveryAgent(build_classifier(request.mode, settings), gateway)
        results = []
        for transaction in transactions:
            classification, idempotency_key = agent.plan(transaction)
            if not repository.reserve_agent_run(transaction, idempotency_key, classification):
                continue

            result = agent.execute_plan(transaction, classification, idempotency_key)
            result.wasted_retry = is_wasted_retry(transaction, result)
            repository.save_agent_result(transaction, result)
            results.append(result)
    except HTTPException:
        raise
    except Exception as exc:
        raise database_error("run the recovery agent", exc) from exc

    summary = summarize_results(request.mode, results)
    return AgentRunResponse(results=results, **summary)


@app.get("/metrics", response_model=Metrics)
def metrics() -> Metrics:
    try:
        return repository.metrics()
    except Exception as exc:
        raise database_error("load metrics", exc) from exc


@app.get("/runs")
def recent_runs(limit: int = 25) -> List[Dict[str, Any]]:
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    try:
        return repository.recent_runs(limit)
    except Exception as exc:
        raise database_error("load recent runs", exc) from exc


@app.post("/gateway/retry", response_model=GatewayResult)
def retry_with_mock_gateway(
    request: GatewayRetryRequest,
    _: Annotated[None, Depends(verify_gateway_token)],
) -> GatewayResult:
    """Authenticated mock-gateway endpoint useful for a local API demonstration."""
    return gateway.execute(request.transaction, request.action, request.idempotency_key)


@app.post("/webhooks/payment", status_code=status.HTTP_202_ACCEPTED)
def receive_payment_webhook(event: WebhookEvent) -> Dict[str, str]:
    """Record a provider webhook once and apply recovered events to matching runs."""
    try:
        repository.ensure_runtime_tables()
        repository.record_webhook(event)
    except Exception as exc:
        raise database_error("record payment webhook", exc) from exc
    return {"status": "accepted", "idempotency_key": event.idempotency_key}
