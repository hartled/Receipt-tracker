"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


@dataclass
class Settings:
    # All persistent state (SQLite DB + uploaded files) lives under DATA_DIR.
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")).resolve())
    database_url: str | None = field(default_factory=lambda: os.getenv("DATABASE_URL"))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    secure_cookies: bool = field(default_factory=lambda: _bool(os.getenv("SECURE_COOKIES"), False))
    # Extraction provider: "heuristic" (default, free, local) | "openai" (optional, needs OPENAI_API_KEY) | "none"
    extractor: str = field(default_factory=lambda: os.getenv("EXTRACTOR", "heuristic").lower())
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None)
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    max_upload_mb: int = field(default_factory=lambda: int(os.getenv("MAX_UPLOAD_MB", "10")))
    # Inbound email webhook stub is disabled unless a shared token is configured.
    inbound_email_token: str | None = field(default_factory=lambda: os.getenv("INBOUND_EMAIL_TOKEN") or None)
    inbound_email_domain: str = field(default_factory=lambda: os.getenv("INBOUND_EMAIL_DOMAIN", "in.example.com"))
    # Dev mode: enables dev-only conveniences, e.g. logging password-reset links when no email provider exists.
    dev_mode: bool = field(default_factory=lambda: _bool(os.getenv("DEV_MODE"), False))
    # Base URL used to build links (password reset). Falls back to the request URL when unset.
    public_base_url: str | None = field(default_factory=lambda: (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/") or None)
    # Login rate limiting (failed attempts are stored in SQLite, so limits survive restarts).
    login_max_failures_per_email: int = field(default_factory=lambda: _int("LOGIN_MAX_FAILURES_PER_EMAIL", 5))
    login_max_failures_per_ip: int = field(default_factory=lambda: _int("LOGIN_MAX_FAILURES_PER_IP", 20))
    login_window_seconds: int = field(default_factory=lambda: _int("LOGIN_WINDOW_SECONDS", 900))
    login_lockout_seconds: int = field(default_factory=lambda: _int("LOGIN_LOCKOUT_SECONDS", 900))
    # Number of trusted reverse proxies in front of the app. 0 = use the socket peer address and ignore
    # X-Forwarded-For. N>0 = use the Nth address from the right of X-Forwarded-For.
    trusted_proxy_hops: int = field(default_factory=lambda: _int("TRUSTED_PROXY_HOPS", 0))
    # Password reset
    reset_token_ttl_minutes: int = field(default_factory=lambda: _int("RESET_TOKEN_TTL_MINUTES", 60))
    reset_max_requests_per_ip: int = field(default_factory=lambda: _int("RESET_MAX_REQUESTS_PER_IP", 10))
    reset_max_requests_per_email: int = field(default_factory=lambda: _int("RESET_MAX_REQUESTS_PER_EMAIL", 3))
    # Plans: free users get N new receipts per calendar month (UTC). Pro is unlimited.
    free_monthly_receipt_limit: int = field(default_factory=lambda: _int("FREE_MONTHLY_RECEIPT_LIMIT", 15))

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        if not self.secret_key:
            # Dev fallback: random per process (sessions reset on restart). Set SECRET_KEY in production.
            self.secret_key = secrets.token_urlsafe(32)
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'receipts.db'}"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
