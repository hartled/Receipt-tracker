"""Login rate limiting, client IP detection, and password-reset tokens."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from starlette.requests import Request

from .config import Settings
from .db import PasswordResetToken, ThrottleEvent, User, as_utc, utcnow


# ---------------------------------------------------------------- client IP
def client_ip(request: Request, trusted_proxy_hops: int) -> str:
    """Socket peer address, or (behind N trusted proxies) the Nth entry from the right of X-Forwarded-For.

    Entries further left can be forged by the client, so we never take the leftmost one blindly."""
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_hops <= 0:
        return peer
    xff = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(xff) >= trusted_proxy_hops:
        return xff[-trusted_proxy_hops]
    return xff[0] if xff else peer


# ---------------------------------------------------------------- throttling
class Throttle:
    """SQLite-backed counters. Works across restarts and multiple worker processes sharing the DB."""

    def count(self, db, key: str, window_seconds: int) -> int:
        since = utcnow() - timedelta(seconds=window_seconds)
        return db.scalar(select(func.count()).select_from(ThrottleEvent)
                         .where(ThrottleEvent.key == key, ThrottleEvent.created_at >= since)) or 0

    def latest(self, db, key: str) -> datetime | None:
        return as_utc(db.scalar(select(func.max(ThrottleEvent.created_at)).where(ThrottleEvent.key == key)))

    def hit(self, db, *keys: str) -> None:
        for k in keys:
            db.add(ThrottleEvent(key=k))
        db.commit()

    def clear(self, db, key: str) -> None:
        db.execute(delete(ThrottleEvent).where(ThrottleEvent.key == key))
        db.commit()

    def prune(self, db, older_than_seconds: int) -> None:
        db.execute(delete(ThrottleEvent).where(ThrottleEvent.created_at < utcnow() - timedelta(seconds=older_than_seconds)))
        db.commit()


class LoginLimiter:
    """Lock out a key (email or IP) after too many failures in a sliding window.

    Once a key has `max` failures within `window`, logins for it are refused until `lockout` seconds after
    its most recent failure. Locked attempts are refused before the password is checked and aren't recorded,
    so the lock simply expires. If window > lockout, each further failure re-locks (a simple backoff)."""

    def __init__(self, settings: Settings, throttle: Throttle | None = None):
        self.s = settings
        self.t = throttle or Throttle()

    def keys(self, email: str, ip: str) -> list[tuple[str, int]]:
        return [(f"login-email:{email}", self.s.login_max_failures_per_email),
                (f"login-ip:{ip}", self.s.login_max_failures_per_ip)]

    def retry_after(self, db, email: str, ip: str) -> int:
        """Seconds until a login attempt is allowed again (0 = allowed now)."""
        wait = 0
        now = utcnow()
        for key, limit in self.keys(email, ip):
            if limit <= 0:
                continue
            if self.t.count(db, key, self.s.login_window_seconds) >= limit:
                last = self.t.latest(db, key)
                if last:
                    remaining = (last + timedelta(seconds=self.s.login_lockout_seconds) - now).total_seconds()
                    wait = max(wait, int(remaining) + 1 if remaining > 0 else 0)
        return wait

    def failure(self, db, email: str, ip: str) -> None:
        self.t.hit(db, *(k for k, _ in self.keys(email, ip)))
        self.t.prune(db, max(self.s.login_window_seconds, self.s.login_lockout_seconds, 3600) * 2)

    def success(self, db, email: str) -> None:
        # Only the per-email counter is cleared; the per-IP one keeps throttling credential stuffing.
        self.t.clear(db, f"login-email:{email}")


# ---------------------------------------------------------------- password reset tokens
def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_reset_token(db, user: User, ttl_minutes: int) -> str:
    """Create a new token for `user` (older unused ones are revoked) and return the raw value.

    Only the SHA-256 hash is stored, so a DB leak doesn't expose usable links."""
    now = utcnow()
    db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == user.id,
                                                PasswordResetToken.used_at.is_(None)).values(used_at=now))
    raw = secrets.token_urlsafe(32)
    db.add(PasswordResetToken(user_id=user.id, token_hash=hash_token(raw), created_at=now,
                              expires_at=now + timedelta(minutes=ttl_minutes)))
    db.commit()
    return raw


def find_valid_token(db, raw: str) -> PasswordResetToken | None:
    if not raw or len(raw) > 200:
        return None
    tok = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(raw)))
    if not tok or tok.used_at is not None or as_utc(tok.expires_at) <= utcnow():
        return None
    return tok


def consume_reset_token(db, raw: str) -> User | None:
    """Atomically mark the token used. Returns its user, or None if invalid/expired/already used."""
    tok = find_valid_token(db, raw)
    if not tok:
        return None
    res = db.execute(update(PasswordResetToken).where(PasswordResetToken.id == tok.id,
                                                      PasswordResetToken.used_at.is_(None))
                     .values(used_at=utcnow()))
    if res.rowcount != 1:  # lost a race with another request using the same link
        db.rollback()
        return None
    # Revoke any other outstanding tokens for this user too.
    db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == tok.user_id,
                                                PasswordResetToken.used_at.is_(None)).values(used_at=utcnow()))
    return db.get(User, tok.user_id)
