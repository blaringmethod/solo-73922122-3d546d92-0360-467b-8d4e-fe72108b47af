"""Runtime configuration.

All values can be overridden through environment variables so the same image
works locally and under Docker Compose.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable application settings."""

    db_path: Path
    seed_demo: bool = False
    busy_timeout_ms: int = 10_000
    title: str = "考古地层序列核定 API"
    version: str = "1.0.0"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.getenv("STRAT_DB_PATH", "./data/strat.db")),
            seed_demo=_env_bool("STRAT_SEED_DEMO", False),
            busy_timeout_ms=int(os.getenv("STRAT_BUSY_TIMEOUT_MS", "10000")),
        )
