from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


load_dotenv(override=True)


@dataclass(frozen=True)
class Settings:
    ibm_archive_dir: Path = Path(os.getenv("IBM_ARCHIVE_DIR", "archive" if Path("archive").exists() else "data"))
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/failed_payments",
    )
    llm_provider: str = os.getenv("LLM_PROVIDER", "gemini").lower()
    gemini_api_key: Optional[str] = os.getenv("GEMINI_API_KEY")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    groq_api_key: Optional[str] = os.getenv("GROQ_API_KEY")
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
    gateway_token: str = os.getenv("GATEWAY_TOKEN", "dev-gateway-token")
    random_seed: int = int(os.getenv("RECOVERY_RANDOM_SEED", "42"))


def get_settings() -> Settings:
    return Settings()
