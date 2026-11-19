# Failed-Payment Recovery Agent: Scope

## Problem

Payments fail for many reasons. Some failures are worth recovering with a retry or customer nudge. Others should be marked lost to avoid wasting retries, annoying users, or creating operational noise.

This project builds a small recovery agent that reads failed credit-card transactions, classifies the failure cause, chooses a recovery action, and later measures whether it beats simple baselines.

## Week 1 Dataset Rewrite

The original roadmap suggested generating synthetic failures. This project uses the downloaded IBM credit-card transaction dataset instead:

- Source folder: the ignored `data/` directory by default, or the directory set in `IBM_ARCHIVE_DIR`
- Main file: `credit_card_transactions-ibm_v2.csv`
- Failed transaction rule: `Errors?` is non-empty
- Local working sample: configurable, default `100000` failed rows

The dataset intentionally keeps rare and combined error types such as `Bad PIN,Technical Glitch`. Those awkward cases are useful because they force the later classifier and agent to handle ambiguity.

## Week 1 Artifact

`recovery_agent/ingest.py` streams the IBM CSV, keeps only failed transactions, samples a laptop-friendly number of rows, derives a recoverability label from the error type, and writes the result to Postgres.

## Ground Truth Label

The first-pass label is deliberately simple and documented in code:

- Recoverable by retry: `Insufficient Balance`, `Technical Glitch`
- Recoverable by customer action: `Bad PIN`, `Bad CVV`, `Bad Zipcode`
- Not recoverable: `Bad Card Number`, `Bad Expiration`
- Mixed rare combinations are preserved and flagged as ambiguous. Hard+soft mixes go to manual review.

This is good enough for a Week 1 evaluation target. Later weeks should review the label policy against real payment-domain assumptions.

## Success Metric

The main business metric is recovered revenue: how much failed payment volume can be recovered without wasting retries on non-recoverable failures.

Early evaluation metrics:

- Recoverable classification accuracy
- Recovery rate on recoverable failures
- Wasted retry rate on non-recoverable failures
- Recovered amount
