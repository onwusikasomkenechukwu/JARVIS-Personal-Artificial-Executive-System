"""Configuration and structured logging.

Secrets and connection strings come from the environment / .env only — never from
code or git (see .gitignore). pydantic-settings validates and types every field.
"""
from __future__ import annotations

import logging

import structlog
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JARVIS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Memory store
    database_url: str = "postgresql://jarvis:jarvis@localhost:5432/jarvis"

    # Browser executor
    browser_headless: bool = True
    browser_timeout_ms: int = 15000
    browser_retries: int = 3

    # Memory
    volatile_ttl_seconds: int = 86400  # 24h

    # Out-of-band confirmation
    pending_confirm_dir: str = "./.pending_confirmations"

    # Credential vault — secret material (e.g. the Gmail token) lives here, OUTSIDE
    # the repo. The orchestrating code path holds a handle, never the raw secret.
    vault_dir: str = r"C:\Users\onwus\.jarvis\vault"

    # Gmail provider state-read (Phase 2). The OAuth client-secret JSON Google issued
    # is discovered by glob (client_secret_*.json) in this dir; the filename is never
    # hardcoded and the secret is never copied into the repo.
    gmail_client_secret_dir: str = r"C:\Users\onwus\.jarvis"

    # Logging
    log_level: str = "INFO"


settings = Settings()


def configure_logging(level: str | None = None) -> None:
    """Emit structured JSON-line logs. Safe to call once at process start."""
    lvl_name = (level or settings.log_level).upper()
    lvl = getattr(logging, lvl_name, logging.INFO)
    logging.basicConfig(format="%(message)s", level=lvl)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "jarvis"):
    return structlog.get_logger(name)
