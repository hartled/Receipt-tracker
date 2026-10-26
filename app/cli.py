"""Admin CLI (no web admin needed). Uses the same env vars as the app (DATA_DIR / DATABASE_URL).

  python -m app.cli set-plan you@example.com pro     # mark a user Pro (testing; no billing exists)
  python -m app.cli set-plan you@example.com free
  python -m app.cli show you@example.com
  python -m app.cli list
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from .config import Settings
from .db import User, make_sessionmaker
from .plans import PLANS, usage


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli", description="Receipt Tracker admin tasks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("set-plan", help="set a user's plan")
    sp.add_argument("email")
    sp.add_argument("plan", choices=sorted(PLANS))
    sh = sub.add_parser("show", help="show a user's plan and this month's usage")
    sh.add_argument("email")
    sub.add_parser("list", help="list users with plan and usage")
    args = ap.parse_args(argv)

    settings = Settings()
    _, Session = make_sessionmaker(settings.database_url)
    with Session() as db:
        if args.cmd == "list":
            for u in db.scalars(select(User).order_by(User.id)):
                us = usage(u, settings)
                print(f"{u.id}\t{u.email}\t{u.plan}\t{us['used']}/{us['limit'] or 'unlimited'} this month")
            return 0
        user = db.scalar(select(User).where(User.email == args.email.strip().lower()))
        if not user:
            print(f"No user with email {args.email}", file=sys.stderr)
            return 1
        if args.cmd == "set-plan":
            user.plan = args.plan
            db.commit()
            print(f"{user.email} is now on the {PLANS[args.plan]} plan.")
        us = usage(user, settings)
        print(f"plan={user.plan} used_this_month={us['used']} limit={us['limit'] or 'unlimited'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
