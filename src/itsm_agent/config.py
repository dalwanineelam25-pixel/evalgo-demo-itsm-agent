"""Application settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

VALID_MODES = ("real", "fixture")
VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(ValueError):
    """Raised when the configuration is invalid."""


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    model_mode: str = "fixture"
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    max_tokens: int = 16000
    model_timeout_s: float = 120.0
    fixture_script: Path | None = None

    db_path: Path = PROJECT_ROOT / "var" / "itsm.db"
    checkpoint_db_path: Path = PROJECT_ROOT / "var" / "checkpoints.db"
    outbox_path: Path = PROJECT_ROOT / "var" / "outbox" / "notifications.jsonl"
    log_path: Path = PROJECT_ROOT / "var" / "logs" / "itsm-agent.log"
    kb_path: Path = DATA_DIR / "kb_articles.json"
    seed_path: Path = DATA_DIR / "seed_data.json"

    diag_base_url: str = "http://127.0.0.1:8765"
    diag_timeout_s: float = 5.0
    diag_retries: int = 1

    recursion_limit: int = 40
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, load_dotenv_file: bool = True) -> "Settings":
        if load_dotenv_file:
            load_dotenv(PROJECT_ROOT / ".env", override=False)
        fixture = os.environ.get("ITSM_FIXTURE_SCRIPT")
        fixture_path = Path(fixture) if fixture else None
        if fixture_path and not fixture_path.is_absolute() and not fixture_path.exists():
            fixture_path = PROJECT_ROOT / fixture_path
        settings = cls(
            model_mode=_env("ITSM_MODEL_MODE", "fixture").lower(),
            model=_env("MODEL", "claude-opus-5-5"),
            effort=_env("MODEL_EFFORT", "medium").lower(),
            max_tokens=int(_env("MODEL_MAX_TOKENS", "16000")),
            model_timeout_s=float(_env("MODEL_TIMEOUT_S", "120")),
            fixture_script=fixture_path,
            db_path=Path(_env("ITSM_DB_PATH", str(PROJECT_ROOT / "var" / "itsm.db"))),
            checkpoint_db_path=Path(
                _env("ITSM_CHECKPOINT_DB_PATH", str(PROJECT_ROOT / "var" / "checkpoints.db"))
            ),
            outbox_path=Path(
                _env("ITSM_OUTBOX_PATH", str(PROJECT_ROOT / "var" / "outbox" / "notifications.jsonl"))
            ),
            log_path=Path(_env("ITSM_LOG_PATH", str(PROJECT_ROOT / "var" / "logs" / "itsm-agent.log"))),
            kb_path=Path(_env("ITSM_KB_PATH", str(DATA_DIR / "kb_articles.json"))),
            seed_path=Path(_env("ITSM_SEED_PATH", str(DATA_DIR / "seed_data.json"))),
            diag_base_url=_env("DIAG_SERVICE_URL", "http://127.0.0.1:8765"),
            diag_timeout_s=float(_env("DIAG_TIMEOUT_S", "5.0")),
            diag_retries=int(_env("DIAG_RETRIES", "1")),
            recursion_limit=int(_env("ITSM_RECURSION_LIMIT", "40")),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.model_mode not in VALID_MODES:
            raise ConfigError(f"ITSM_MODEL_MODE must be one of {VALID_MODES}, got {self.model_mode!r}")
        if self.effort not in VALID_EFFORTS:
            raise ConfigError(f"MODEL_EFFORT must be one of {VALID_EFFORTS}, got {self.effort!r}")
        if self.diag_timeout_s <= 0:
            raise ConfigError("DIAG_TIMEOUT_S must be positive")
        if self.diag_retries < 0:
            raise ConfigError("DIAG_RETRIES must be >= 0")
