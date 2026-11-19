from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .config import Settings
from .rules import classify_with_rules
from .schemas import Classification, Transaction


CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cause": {"type": "string"},
        "recoverable": {"type": "boolean"},
        "confidence": {"type": "number"},
        "action": {
            "type": "string",
            "enum": ["retry_now", "schedule_retry", "notify_customer", "mark_lost", "manual_review"],
        },
        "reason": {"type": "string"},
        "ambiguous": {"type": "boolean"},
    },
    "required": ["cause", "recoverable", "confidence", "action", "reason", "ambiguous"],
}


SYSTEM_INSTRUCTION = """
You classify failed credit-card payments for a recovery agent.
Return only JSON matching the provided schema.
Use these actions:
- retry_now: transient gateway or bank issue.
- schedule_retry: likely temporary funds issue.
- notify_customer: customer can correct PIN, CVV, ZIP, or payment details.
- mark_lost: hard decline unlikely to recover.
- manual_review: truly unclear cases.
Rare mixed errors should stay ambiguous, not be hidden.
"""


class LLMUnavailable(RuntimeError):
    pass


class LLMClassifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    def classify(self, transaction: Transaction) -> Classification:
        if self.settings.llm_provider == "groq":
            raw = self._classify_with_groq(transaction)
        elif self.settings.llm_provider == "gemini":
            raw = self._classify_with_gemini(transaction)
        else:
            raise LLMUnavailable(f"Unsupported LLM_PROVIDER={self.settings.llm_provider!r}")

        return self._parse_classification(raw, transaction)

    def _classify_with_gemini(self, transaction: Transaction) -> dict[str, Any]:
        if not self.settings.gemini_api_key:
            raise LLMUnavailable("GEMINI_API_KEY is not set.")

        response = self._post_json(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            headers={
                "x-goog-api-key": self.settings.gemini_api_key,
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.gemini_model,
                "system_instruction": SYSTEM_INSTRUCTION,
                "input": self._prompt(transaction),
                "generation_config": {"temperature": 0.0},
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": CLASSIFICATION_SCHEMA,
                },
            },
        )
        payload = response.json()
        text = payload.get("output_text") or self._extract_gemini_text(payload)
        return json.loads(text)

    def _classify_with_groq(self, transaction: Transaction) -> dict[str, Any]:
        if not self.settings.groq_api_key:
            raise LLMUnavailable("GROQ_API_KEY is not set.")

        response = self._post_json(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.settings.groq_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.settings.groq_model,
                "messages": [
                    {"role": "system", "content": SYSTEM_INSTRUCTION},
                    {"role": "user", "content": self._prompt(transaction)},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        payload = response.json()
        text = payload["choices"][0]["message"]["content"]
        return json.loads(text)

    def _parse_classification(self, raw: dict[str, Any], transaction: Transaction) -> Classification:
        fallback = classify_with_rules(transaction, classifier_name="rules_fallback")
        return Classification(
            cause=str(raw.get("cause") or fallback.cause),
            recoverable=self._parse_bool(raw.get("recoverable", fallback.recoverable)),
            confidence=max(0.0, min(1.0, float(raw.get("confidence", fallback.confidence)))),
            action=str(raw.get("action") or fallback.action),
            classifier=f"llm:{self.settings.llm_provider}",
            reason=str(raw.get("reason") or fallback.reason),
            ambiguous=self._parse_bool(raw.get("ambiguous", fallback.ambiguous)),
        )

    def _parse_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        raise LLMUnavailable(f"Expected boolean from LLM, got {value!r}.")

    def _prompt(self, transaction: Transaction) -> str:
        return json.dumps(
            {
                "transaction_id": transaction.id,
                "amount": str(transaction.amount),
                "use_chip": transaction.use_chip,
                "merchant_city": transaction.merchant_city,
                "merchant_state": transaction.merchant_state,
                "mcc": transaction.mcc,
                "errors_raw": transaction.errors_raw,
                "error_types": transaction.error_types,
                "is_fraud": transaction.is_fraud,
            },
            sort_keys=True,
        )

    def _extract_gemini_text(self, payload: dict[str, Any]) -> str:
        chunks: list[str] = []
        for step in payload.get("steps", []):
            for content in step.get("content", []):
                if isinstance(content, dict) and content.get("type") == "text":
                    chunks.append(content.get("text", ""))
        if chunks:
            return "".join(chunks)
        raise LLMUnavailable("Gemini response did not include output_text.")

    def _post_json(
        self,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any],
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = httpx.post(url, headers=headers, json=json, timeout=30)
                if response.status_code >= 500 and attempt < 2:
                    time.sleep(0.25 * (2**attempt))
                    continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise LLMUnavailable("LLM request failed after retries.")


class SafeLLMClassifier:
    def __init__(self, settings: Settings):
        self.llm = LLMClassifier(settings)

    def classify(self, transaction: Transaction) -> Classification:
        try:
            return self.llm.classify(transaction)
        except Exception as exc:  # The app should still demo without a valid LLM key.
            fallback = classify_with_rules(transaction, classifier_name="rules_fallback")
            fallback.reason = f"{fallback.reason} LLM unavailable: {exc}"
            return fallback
