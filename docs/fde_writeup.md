# Failed-Payment Recovery Agent Write-Up

## Problem

Failed payments are not all equal. Some are soft declines that can recover with a retry or customer nudge. Others are hard declines where retrying wastes gateway calls, adds noise, and risks a bad user experience.

The project builds a recovery agent that reads failed transactions, classifies the root cause, chooses a recovery action, and measures recovered revenue against simple baselines.

## Dataset

The project uses the IBM credit-card transaction dataset from the local archive instead of synthetic failures. A row is treated as failed when the `Errors?` column is non-empty.

This creates a more realistic dataset because it includes skewed distributions and rare combined error types. In the full transaction file, there are 388,431 failed rows and 23 distinct error strings.

## Scope

The system focuses on one loop:

1. Load failed payments into Postgres.
2. Classify the failure root cause.
3. Decide a recovery action.
4. Execute the action against a mock gateway tool.
5. Record outcomes and measure recovered revenue.

The project deliberately avoids a large frontend, vector database, or unnecessary framework. The depth is in the data, action loop, idempotency, webhook records, and evaluation.

## Classifiers

The project supports three classifier modes:

- `retry_all`: a dumb baseline that retries every failed payment.
- `rules`: a deterministic rules baseline based on the IBM `Errors?` value.
- `llm`: a Gemini or Groq classifier that returns structured JSON and falls back to rules if the API key is missing or the call fails.

## Recovery Actions

The agent can choose:

- `retry_now` for transient technical issues.
- `schedule_retry` for insufficient balance cases.
- `notify_customer` for customer-fixable issues such as PIN, CVV, or ZIP.
- `mark_lost` for hard declines.
- `manual_review` for unclear cases.

## Mock Gateway

The mock gateway is deterministic. It simulates realistic recovery probabilities while ensuring the same transaction and idempotency key produce the same outcome each run. That makes evaluation repeatable.

The default agent uses this gateway in process so the local demo has fewer moving pieces. The FastAPI app also exposes authenticated mock gateway and webhook endpoints for showing the external API shape.

## Metrics

The key business metric is recovered revenue. Supporting metrics are:

- Recovery rate on recoverable failures.
- Wasted retry rate on non-recoverable failures.
- Classification accuracy against the derived ground-truth label.
- Total recovered amount.

## Tradeoffs

The recoverability label is inferred from error type, not confirmed by real bank outcomes. That is acceptable for a portfolio project because it creates a clear evaluation target, but a production version would calibrate labels with actual retry success data.

The LLM path is optional because running an API call for every failed transaction can be slow and expensive. The rule fallback keeps the system demoable without external dependencies.

`Bad PIN`, `Bad CVV`, and `Bad Zipcode` are treated as customer-action recoveries, not blind retry recoveries. Mixed hard+soft cases are preserved as ambiguous and routed to manual review.

## Production Extensions

If this were run for real merchants, the next steps would be:

- Learn merchant-specific recovery probabilities from historical outcomes.
- Add rate limits and retry budgets per customer and card.
- Split webhook handling into a separate public endpoint.
- Add audit trails for every agent decision.
- Use real provider decline codes instead of derived IBM labels.
- Add dashboards by merchant, payment method, and failure cohort.
