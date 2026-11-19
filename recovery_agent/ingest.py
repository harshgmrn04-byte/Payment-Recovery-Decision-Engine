from __future__ import annotations

import argparse
import csv
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover - optional convenience dependency
    load_dotenv = None

try:
    import psycopg
except ImportError:  # pragma: no cover - handled at runtime for dry runs/tests
    psycopg = None


DEFAULT_ARCHIVE_DIR = Path("archive") if Path("archive").exists() else Path("data")
DEFAULT_TRANSACTIONS_FILE = "credit_card_transactions-ibm_v2.csv"
DEFAULT_TABLE = "failed_transactions"
TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

RECOVERABLE_ERRORS = {
    "Insufficient Balance",
    "Technical Glitch",
    "Bad PIN",
    "Bad CVV",
    "Bad Zipcode",
}

NON_RECOVERABLE_ERRORS = {
    "Bad Card Number",
    "Bad Expiration",
}


@dataclass(frozen=True)
class PreparedTransaction:
    source_user_id: int
    source_card_id: int
    transaction_ts: datetime
    amount: Decimal
    use_chip: str
    merchant_name: str
    merchant_city: str
    merchant_state: str | None
    merchant_zip: str | None
    mcc: str
    errors_raw: str
    error_types: list[str]
    recoverable: bool
    recovery_action: str
    is_ambiguous: bool
    is_fraud: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare failed IBM credit-card transactions for Postgres."
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=Path(os.getenv("IBM_ARCHIVE_DIR", "archive")),
        help="Folder containing the IBM dataset CSV files.",
    )
    parser.add_argument(
        "--transactions-file",
        default=DEFAULT_TRANSACTIONS_FILE,
        help="Transactions CSV filename inside --archive-dir.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=100_000,
        help="Number of failed transactions to sample.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible sampling.",
    )
    parser.add_argument(
        "--table",
        default=DEFAULT_TABLE,
        help="Postgres table to write.",
    )
    parser.add_argument(
        "--if-exists",
        choices=("replace", "append", "fail"),
        default="replace",
        help="What to do when the target table exists.",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/failed_payments",
        ),
        help="Postgres connection URL. Defaults to DATABASE_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read, sample, and print stats without writing to Postgres.",
    )
    return parser.parse_args()


def split_errors(errors_raw: str) -> list[str]:
    return [part.strip() for part in errors_raw.split(",") if part.strip()]


def label_recoverability(error_types: Iterable[str]) -> tuple[bool, str, bool]:
    errors = set(error_types)
    recoverable_hits = errors & RECOVERABLE_ERRORS
    non_recoverable_hits = errors & NON_RECOVERABLE_ERRORS
    unknown_hits = errors - RECOVERABLE_ERRORS - NON_RECOVERABLE_ERRORS
    is_ambiguous = bool(unknown_hits or (recoverable_hits and non_recoverable_hits))

    if recoverable_hits and non_recoverable_hits:
        return False, "manual_review", True
    if "Technical Glitch" in errors:
        return True, "retry_now", is_ambiguous
    if "Insufficient Balance" in errors:
        return True, "schedule_retry", is_ambiguous
    if errors & {"Bad PIN", "Bad CVV", "Bad Zipcode"}:
        return True, "notify_customer", is_ambiguous
    if errors & {"Bad Card Number", "Bad Expiration"}:
        return False, "mark_lost", is_ambiguous

    return False, "manual_review", True


def parse_amount(value: str) -> Decimal:
    normalized = value.replace("$", "").replace(",", "").strip()
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount value: {value!r}") from exc


def parse_timestamp(row: dict[str, str]) -> datetime:
    raw = f"{row['Year']}-{row['Month']}-{row['Day']} {row['Time']}"
    return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def prepare_row(row: dict[str, str]) -> PreparedTransaction | None:
    errors_raw = (row.get("Errors?") or "").strip()
    if not errors_raw:
        return None

    error_types = split_errors(errors_raw)
    recoverable, recovery_action, is_ambiguous = label_recoverability(error_types)

    return PreparedTransaction(
        source_user_id=int(row["User"]),
        source_card_id=int(row["Card"]),
        transaction_ts=parse_timestamp(row),
        amount=parse_amount(row["Amount"]),
        use_chip=row["Use Chip"].strip(),
        merchant_name=row["Merchant Name"].strip(),
        merchant_city=row["Merchant City"].strip(),
        merchant_state=optional_str(row.get("Merchant State")),
        merchant_zip=optional_str(row.get("Zip")),
        mcc=row["MCC"].strip(),
        errors_raw=errors_raw,
        error_types=error_types,
        recoverable=recoverable,
        recovery_action=recovery_action,
        is_ambiguous=is_ambiguous,
        is_fraud=(row.get("Is Fraud?") or "").strip().lower() == "yes",
    )


def reservoir_sample_failed(csv_path: Path, sample_size: int, seed: int) -> tuple[list[PreparedTransaction], int]:
    if sample_size < 1:
        raise ValueError("--sample-size must be at least 1")

    rng = random.Random(seed)
    sample: list[PreparedTransaction] = []
    failed_count = 0

    with csv_path.open(newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            prepared = prepare_row(row)
            if prepared is None:
                continue

            failed_count += 1
            if len(sample) < sample_size:
                sample.append(prepared)
                continue

            replacement_index = rng.randint(0, failed_count - 1)
            if replacement_index < sample_size:
                sample[replacement_index] = prepared

    return sample, failed_count


def create_table_sql(table: str) -> str:
    validate_table_name(table)
    return f"""
    CREATE TABLE {table} (
        id BIGSERIAL PRIMARY KEY,
        source_user_id INTEGER NOT NULL,
        source_card_id INTEGER NOT NULL,
        transaction_ts TIMESTAMPTZ NOT NULL,
        amount NUMERIC(12, 2) NOT NULL,
        use_chip TEXT NOT NULL,
        merchant_name TEXT NOT NULL,
        merchant_city TEXT NOT NULL,
        merchant_state TEXT,
        merchant_zip TEXT,
        mcc TEXT NOT NULL,
        errors_raw TEXT NOT NULL,
        error_types TEXT[] NOT NULL,
        recoverable BOOLEAN NOT NULL,
        recovery_action TEXT NOT NULL,
        is_ambiguous BOOLEAN NOT NULL,
        is_fraud BOOLEAN NOT NULL,
        loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """


def ensure_table(conn: "psycopg.Connection", table: str, if_exists: str) -> None:
    validate_table_name(table)
    with conn.cursor() as cur:
        exists = cur.execute(
            "SELECT to_regclass(%s)",
            (table,),
        ).fetchone()[0]

        if exists and if_exists == "fail":
            raise RuntimeError(f"Table {table!r} already exists. Use --if-exists replace or append.")
        if exists and if_exists == "replace":
            # Identifier interpolation is safe here because validate_table_name
            # restricts this to a single bare SQL identifier.
            cur.execute(f"DROP TABLE {table}")

        if not exists or if_exists == "replace":
            cur.execute(create_table_sql(table))

    conn.commit()


def write_to_postgres(rows: list[PreparedTransaction], database_url: str, table: str, if_exists: str) -> None:
    if psycopg is None:
        raise RuntimeError(
            "Missing dependency psycopg. Activate .venv and run: "
            "python -m pip install -r requirements.txt"
        )

    with psycopg.connect(database_url) as conn:
        ensure_table(conn, table, if_exists)
        with conn.cursor() as cur:
            # Identifier interpolation is safe here because validate_table_name
            # already restricts this to a single bare SQL identifier.
            cur.executemany(
                f"""
                INSERT INTO {table} (
                    source_user_id, source_card_id, transaction_ts, amount, use_chip,
                    merchant_name, merchant_city, merchant_state, merchant_zip, mcc,
                    errors_raw, error_types, recoverable, recovery_action,
                    is_ambiguous, is_fraud
                )
                VALUES (
                    %(source_user_id)s, %(source_card_id)s, %(transaction_ts)s, %(amount)s, %(use_chip)s,
                    %(merchant_name)s, %(merchant_city)s, %(merchant_state)s, %(merchant_zip)s, %(mcc)s,
                    %(errors_raw)s, %(error_types)s, %(recoverable)s, %(recovery_action)s,
                    %(is_ambiguous)s, %(is_fraud)s
                )
                """,
                [row.__dict__ for row in rows],
            )
        conn.commit()


def validate_table_name(table: str) -> None:
    if not TABLE_NAME_PATTERN.match(table):
        raise ValueError(
            "Table name must contain only letters, numbers, and underscores, "
            "and cannot start with a number."
        )


def print_summary(rows: list[PreparedTransaction], failed_count: int) -> None:
    recoverable_count = sum(row.recoverable for row in rows)
    ambiguous_count = sum(row.is_ambiguous for row in rows)
    total_amount = sum((row.amount for row in rows), Decimal("0"))

    print(f"Failed rows found: {failed_count:,}")
    print(f"Sample rows prepared: {len(rows):,}")
    print(f"Recoverable in sample: {recoverable_count:,}")
    print(f"Non-recoverable in sample: {len(rows) - recoverable_count:,}")
    print(f"Ambiguous in sample: {ambiguous_count:,}")
    print(f"Sample failed amount: ${total_amount:,.2f}")


def find_transactions_csv(archive_dir: Path, filename: str) -> Path:
    candidates = [
        archive_dir / filename,
        Path.cwd() / "archive" / filename,
        Path(__file__).resolve().parent.parent / "archive" / filename,
        Path.cwd() / "data" / filename,
        Path(__file__).resolve().parent.parent / "data" / filename,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return archive_dir / filename


def main() -> None:
    if load_dotenv is not None:
        load_dotenv(override=True)

    args = parse_args()
    csv_path = find_transactions_csv(args.archive_dir, args.transactions_file)
    if not csv_path.exists():
        raise FileNotFoundError(f"Transactions CSV not found: {csv_path}")

    print(f"Reading dataset from: {csv_path.resolve()}")
    rows, failed_count = reservoir_sample_failed(csv_path, args.sample_size, args.seed)
    print_summary(rows, failed_count)

    if args.dry_run:
        print("Dry run complete. No Postgres writes performed.")
        return

    if not args.database_url:
        raise RuntimeError("DATABASE_URL is required unless --dry-run is used.")

    write_to_postgres(rows, args.database_url, args.table, args.if_exists)
    print(f"Wrote {len(rows):,} rows to Postgres table {args.table!r}.")


if __name__ == "__main__":
    main()
