"""Plans and the free-tier monthly receipt limit. No billing: plans are changed with `python -m app.cli`."""
from __future__ import annotations

import calendar
from datetime import date

from sqlalchemy import update

from .config import Settings
from .db import User, utcnow

FREE, PRO = "free", "pro"
PLANS = {FREE: "Free", PRO: "Pro"}
PRO_PRICE_MONTHLY, PRO_PRICE_YEARLY = 6, 49


class QuotaExceeded(Exception):
    pass


def current_month() -> str:
    return utcnow().strftime("%Y-%m")


def next_reset_date() -> date:
    now = utcnow().date()
    return date(now.year + (now.month == 12), now.month % 12 + 1, 1)


def usage(user: User, settings: Settings) -> dict:
    month = current_month()
    used = user.usage_count if user.usage_month == month else 0
    unlimited = user.plan == PRO
    limit = None if unlimited else settings.free_monthly_receipt_limit
    y, m = map(int, month.split("-"))
    return {"plan": user.plan, "plan_label": PLANS.get(user.plan, user.plan), "used": used, "limit": limit,
            "unlimited": unlimited, "remaining": None if unlimited else max(0, limit - used),
            "at_limit": (not unlimited) and used >= limit, "month_label": f"{calendar.month_name[m]} {y}",
            "resets_on": next_reset_date()}


def reserve_slot(db, user: User, settings: Settings) -> None:
    """Count one new receipt against the user's month. Raises QuotaExceeded for free users at the limit.

    Uses conditional UPDATEs so concurrent uploads can't push a free user past the limit."""
    month = current_month()
    db.execute(update(User).where(User.id == user.id, User.usage_month != month)
               .values(usage_month=month, usage_count=0))
    q = update(User).where(User.id == user.id, User.usage_month == month)
    if user.plan != PRO:
        q = q.where(User.usage_count < settings.free_monthly_receipt_limit)
    res = db.execute(q.values(usage_count=User.usage_count + 1))
    db.commit()
    db.refresh(user)
    if res.rowcount != 1:
        raise QuotaExceeded()


def limit_message(settings: Settings) -> str:
    d = next_reset_date()
    return (f"You've used all {settings.free_monthly_receipt_limit} free receipts for this month. "
            f"Your limit resets on {d:%B} 1. See the Upgrade page for Pro (unlimited receipts).")
