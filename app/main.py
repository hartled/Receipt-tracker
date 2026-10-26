"""Receipt Tracker — FastAPI app (MVP)."""
from __future__ import annotations

import base64
import binascii
import hmac
import logging
import re
import secrets
import shutil
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import bcrypt
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import extract, func, select
from starlette.middleware.sessions import SessionMiddleware

from . import categories as cats
from .config import Settings
from . import plans
from .db import Receipt, User, make_sessionmaker
from .mailer import build_mailer
from .security import LoginLimiter, Throttle, client_ip, consume_reset_token, find_valid_token, issue_reset_token
from .exports import DISCLAIMER, summarize, to_csv, to_pdf
from .extraction import build_extractor, tesseract_available

log = logging.getLogger("receipt_tracker")
BASE = Path(__file__).parent
ALLOWED = {  # magic bytes -> (content type, extension)
    b"%PDF": ("application/pdf", ".pdf"),
    b"\x89PNG": ("image/png", ".png"),
    b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
}
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["money"] = lambda c: f"${(c or 0) / 100:,.2f}"


class RedactTokens(logging.Filter):
    """Strip password-reset tokens from access-log lines (uvicorn logs full request paths)."""
    _re = re.compile(r"(token=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(self._re.sub(r"\1[redacted]", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = self._re.sub(r"\1[redacted]", record.msg)
        return True


_access_log = logging.getLogger("uvicorn.access")
if not any(isinstance(f, RedactTokens) for f in _access_log.filters):
    _access_log.addFilter(RedactTokens())


class LoginRequired(Exception):
    pass


def sniff(head: bytes) -> tuple[str, str] | None:
    for magic, info in ALLOWED.items():
        if head.startswith(magic):
            return info
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ("image/webp", ".webp")
    return None


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode()[:72], bcrypt.gensalt()).decode()


# Used to spend the same bcrypt time when an email doesn't exist, so timing doesn't reveal accounts.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()


def check_pw(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], h.encode())
    except ValueError:
        return False


def parse_money(s: str) -> int:
    s = (s or "").replace("$", "").replace(",", "").strip()
    if not s:
        return 0
    try:
        return round(float(s) * 100)
    except ValueError:
        raise HTTPException(400, "Invalid amount")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    engine, SessionLocal = make_sessionmaker(settings.database_url)
    extractor = build_extractor(settings)
    mailer = build_mailer(settings)
    throttle = Throttle()
    limiter = LoginLimiter(settings, throttle)

    app = FastAPI(title="Receipt Tracker", docs_url=None, redoc_url=None)
    app.state.settings, app.state.engine, app.state.extractor = settings, engine, extractor
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="rt_session",
                       max_age=14 * 24 * 3600, same_site="lax", https_only=settings.secure_cookies)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    # ------------------------------------------------------------ helpers
    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def current_user(request: Request, db=Depends(get_db)) -> User:
        uid = request.session.get("uid")
        user = db.get(User, uid) if uid else None
        if user and request.session.get("epoch", 0) != (user.session_epoch or 0):
            user = None  # password was reset since this session was created
        if not user:
            request.session.clear()
            raise LoginRequired()
        return user

    def csrf_token(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = request.session["csrf"] = secrets.token_urlsafe(24)
        return tok

    def check_csrf(request: Request, token: str) -> None:
        if not token or not hmac.compare_digest(token, request.session.get("csrf", "")):
            raise HTTPException(400, "Invalid form token; reload the page and try again.")

    def flash(request: Request, msg: str, kind: str = "info") -> None:
        # Reassign (not .append) so the session is marked modified and the cookie is re-sent.
        request.session["flash"] = [*request.session.get("flash", []), [kind, msg]]

    def render(request: Request, name: str, ctx: dict | None = None, status: int = 200, user: User | None = None):
        ctx = dict(ctx or {})
        ctx.update(user=user, csrf=csrf_token(request), flashes=request.session.pop("flash", []),
                   disclaimer=DISCLAIMER, categories=cats.CATEGORIES, cat=cats.get)
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def user_dir(user: User) -> Path:
        d = settings.upload_dir / str(user.id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def owned_receipt(db, user: User, rid: int) -> Receipt:
        r = db.get(Receipt, rid)
        if not r or r.user_id != user.id:  # never reveal other users' receipts
            raise HTTPException(404, "Not found")
        return r

    def validate_file(data: bytes) -> tuple[str, str]:
        max_bytes = settings.max_upload_mb * 1024 * 1024
        if len(data) > max_bytes:
            raise HTTPException(413, f"File too large (max {settings.max_upload_mb} MB)")
        info = sniff(data[:16])
        if not info:
            raise HTTPException(415, "Unsupported file type. Upload a JPG, PNG, WEBP or PDF.")
        return info

    def store_file(db, user: User, data: bytes) -> tuple[str, str]:
        ctype, ext = validate_file(data)
        plans.reserve_slot(db, user, settings)  # raises plans.QuotaExceeded on the free-tier limit
        stored = f"{uuid.uuid4().hex}{ext}"
        (user_dir(user) / stored).write_bytes(data)
        return stored, ctype

    def create_receipt_from_file(db, user: User, data: bytes, original_name: str | None, source_hint: str = "") -> Receipt:
        stored, ctype = store_file(db, user, data)
        res = extractor.extract(user_dir(user) / stored, ctype)
        r = Receipt(user_id=user.id, stored_name=stored, original_name=(original_name or "")[:255],
                    content_type=ctype, vendor=res.vendor or "", date=res.date,
                    amount_cents=res.amount_cents or 0, raw_text=res.raw_text,
                    category=cats.suggest_category(res.vendor, res.raw_text),
                    extraction_source=(source_hint + res.source) if source_hint else res.source, needs_review=1)
        db.add(r)
        db.commit()
        return r

    def years_for(db, user: User) -> list[int]:
        rows = db.execute(select(extract("year", Receipt.date)).where(Receipt.user_id == user.id,
                                                                     Receipt.date.is_not(None)).distinct()).all()
        ys = {int(y) for (y,) in rows if y}
        ys.add(date.today().year)
        return sorted(ys, reverse=True)

    def query_receipts(db, user: User, year: int | None, category: str | None):
        q = select(Receipt).where(Receipt.user_id == user.id)
        if year:
            q = q.where(extract("year", Receipt.date) == year)
        if category:
            q = q.where(Receipt.category == category)
        return db.scalars(q.order_by(Receipt.date.desc().nulls_last(), Receipt.id.desc())).all()

    def parse_year(v: str | None) -> int | None:
        return int(v) if v and v.isdigit() else None

    # ------------------------------------------------------------ public pages
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        if request.session.get("uid"):
            return RedirectResponse("/dashboard", status_code=303)
        return render(request, "landing.html")

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request, db=Depends(get_db)):
        uid = request.session.get("uid")
        return render(request, "privacy.html", {"settings": settings}, user=db.get(User, uid) if uid else None)

    @app.get("/signup", response_class=HTMLResponse)
    def signup_form(request: Request):
        return render(request, "auth.html", {"mode": "signup"})

    @app.post("/signup")
    def signup(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form(""),
               db=Depends(get_db)):
        check_csrf(request, csrf)
        email = email.strip().lower()
        if "@" not in email or len(email) > 255:
            return render(request, "auth.html", {"mode": "signup", "error": "Enter a valid email."}, 400)
        if len(password) < 8:
            return render(request, "auth.html", {"mode": "signup", "error": "Password must be at least 8 characters.",
                                                 "email": email}, 400)
        if db.scalar(select(User).where(User.email == email)):
            return render(request, "auth.html", {"mode": "signup", "error": "An account with that email exists."}, 400)
        user = User(email=email, password_hash=hash_pw(password), inbound_alias=secrets.token_hex(8))
        db.add(user)
        db.commit()
        request.session.clear()
        request.session["uid"], request.session["epoch"] = user.id, user.session_epoch or 0
        flash(request, "Welcome! Upload your first receipt.", "success")
        return RedirectResponse("/dashboard", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "auth.html", {"mode": "login"})

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...), csrf: str = Form(""),
              db=Depends(get_db)):
        check_csrf(request, csrf)
        norm = email.strip().lower()[:255]
        ip = client_ip(request, settings.trusted_proxy_hops)
        wait = limiter.retry_after(db, norm, ip)
        if wait:
            minutes = max(1, (wait + 59) // 60)
            resp = render(request, "auth.html", {"mode": "login", "email": email,
                          "error": f"Too many failed login attempts. Try again in about {minutes} minute(s), "
                                   "or reset your password."}, 429)
            resp.headers["Retry-After"] = str(wait)
            return resp
        user = db.scalar(select(User).where(User.email == norm))
        ok = check_pw(password, user.password_hash if user else _DUMMY_HASH) and user is not None
        if not ok:
            limiter.failure(db, norm, ip)
            return render(request, "auth.html", {"mode": "login", "error": "Wrong email or password.",
                                                 "email": email}, 400)
        limiter.success(db, norm)
        request.session.clear()  # rotate session on login
        request.session["uid"], request.session["epoch"] = user.id, user.session_epoch or 0
        return RedirectResponse("/dashboard", status_code=303)

    # ------------------------------------------------------------ password reset
    def reset_link(request: Request, raw: str) -> str:
        base = settings.public_base_url or str(request.base_url).rstrip("/")
        return f"{base}/reset-password?token={raw}"

    @app.get("/forgot-password", response_class=HTMLResponse)
    def forgot_form(request: Request):
        return render(request, "forgot.html")

    @app.post("/forgot-password")
    def forgot(request: Request, email: str = Form(...), csrf: str = Form(""), db=Depends(get_db)):
        """Always shows the same response, whether or not the email has an account."""
        check_csrf(request, csrf)
        norm = email.strip().lower()[:255]
        ip = client_ip(request, settings.trusted_proxy_hops)
        ip_key, email_key = f"reset-ip:{ip}", f"reset-email:{norm}"
        allowed = (throttle.count(db, ip_key, 3600) < settings.reset_max_requests_per_ip
                   and throttle.count(db, email_key, 3600) < settings.reset_max_requests_per_email)
        throttle.hit(db, ip_key, email_key)
        user = db.scalar(select(User).where(User.email == norm)) if allowed and "@" in norm else None
        if user:
            raw = issue_reset_token(db, user, settings.reset_token_ttl_minutes)
            link = reset_link(request, raw)
            body = (f"Someone asked to reset the password for your Receipt Tracker account.\n\n{link}\n\n"
                    f"The link works once and expires in {settings.reset_token_ttl_minutes} minutes. "
                    "If you didn't ask for this, ignore this email.")
            if mailer.configured:
                mailer.send(user.email, "Reset your Receipt Tracker password", body)
            elif settings.dev_mode:
                log.warning("DEV_MODE: no email provider configured. Password reset link for %s: %s",
                            user.email, link)
            else:
                log.warning("Password reset requested but no email provider is configured; link not delivered. "
                            "Set DEV_MODE=true locally to log reset links.")
        elif not allowed:
            log.info("password reset request throttled")
        return render(request, "forgot.html", {"sent": True})

    @app.get("/reset-password", response_class=HTMLResponse)
    def reset_form(request: Request, token: str = "", db=Depends(get_db)):
        valid = find_valid_token(db, token) is not None
        return render(request, "reset.html", {"token": token if valid else "", "invalid": not valid},
                      200 if valid else 400)

    @app.post("/reset-password")
    def reset(request: Request, token: str = Form(""), password: str = Form(...), confirm: str = Form(...),
              csrf: str = Form(""), db=Depends(get_db)):
        check_csrf(request, csrf)
        if find_valid_token(db, token) is None:
            return render(request, "reset.html", {"invalid": True}, 400)
        if len(password) < 8 or password != confirm:
            err = "Password must be at least 8 characters." if len(password) < 8 else "Passwords don't match."
            return render(request, "reset.html", {"token": token, "error": err}, 400)
        user = consume_reset_token(db, token)
        if not user:
            return render(request, "reset.html", {"invalid": True}, 400)
        user.password_hash = hash_pw(password)
        user.session_epoch = (user.session_epoch or 0) + 1  # log out every existing session
        db.commit()
        limiter.success(db, user.email)
        request.session.clear()
        flash(request, "Your password was reset. Log in with your new password.", "success")
        return RedirectResponse("/login", status_code=303)

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------ app pages
    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, year: str | None = None, user: User = Depends(current_user), db=Depends(get_db)):
        y = parse_year(year) or date.today().year
        receipts = query_receipts(db, user, y, None)
        undated = db.scalar(select(func.count()).select_from(Receipt).where(Receipt.user_id == user.id,
                                                                            Receipt.date.is_(None)))
        return render(request, "dashboard.html", {"year": y, "years": years_for(db, user),
                                                  "summary": summarize(receipts), "undated": undated,
                                                  "recent": receipts[:5], "usage": plans.usage(user, settings)},
                      user=user)

    @app.get("/receipts", response_class=HTMLResponse)
    def receipts_list(request: Request, year: str | None = None, category: str | None = None,
                      user: User = Depends(current_user), db=Depends(get_db)):
        y = parse_year(year)
        c = category if category in cats.BY_KEY else None
        rows = query_receipts(db, user, y, c)
        return render(request, "receipts.html", {"receipts": rows, "year": y, "category": c,
                                                 "years": years_for(db, user),
                                                 "total": sum(r.amount_cents for r in rows)}, user=user)

    @app.get("/receipts/new", response_class=HTMLResponse)
    def upload_form(request: Request, user: User = Depends(current_user)):
        return render(request, "upload.html", {"extractor": extractor.name, "ocr": tesseract_available(),
                                               "usage": plans.usage(user, settings)}, user=user)

    @app.post("/receipts/new")
    async def upload(request: Request, files: list[UploadFile] = File(default=[]), csrf: str = Form(""),
                     user: User = Depends(current_user), db=Depends(get_db)):
        check_csrf(request, csrf)
        files = [f for f in files if f and f.filename]
        if not files:  # manual entry without a file
            try:
                plans.reserve_slot(db, user, settings)
            except plans.QuotaExceeded:
                flash(request, plans.limit_message(settings), "error")
                return RedirectResponse("/receipts/new", status_code=303)
            r = Receipt(user_id=user.id, extraction_source="manual")
            db.add(r)
            db.commit()
            return RedirectResponse(f"/receipts/{r.id}/edit", status_code=303)
        created, skipped = [], 0
        for f in files[:20]:
            data = await f.read(settings.max_upload_mb * 1024 * 1024 + 1)
            try:
                created.append(create_receipt_from_file(db, user, data, f.filename))
            except plans.QuotaExceeded:
                skipped += 1
        if skipped:
            if not created:
                flash(request, plans.limit_message(settings), "error")
                return RedirectResponse("/receipts/new", status_code=303)
            flash(request, f"Uploaded {len(created)} receipt(s). Skipped {skipped} file(s): you reached this "
                           f"month's free limit of {settings.free_monthly_receipt_limit} receipts. See the Upgrade "
                           "page for Pro.", "error")
            return RedirectResponse("/receipts", status_code=303)
        if len(created) == 1:
            flash(request, "Receipt uploaded. Check the extracted details and save.", "success")
            return RedirectResponse(f"/receipts/{created[0].id}/edit", status_code=303)
        flash(request, f"{len(created)} receipts uploaded. Review each one marked 'needs review'.", "success")
        return RedirectResponse("/receipts", status_code=303)

    @app.get("/receipts/{rid}/edit", response_class=HTMLResponse)
    def edit_form(rid: int, request: Request, user: User = Depends(current_user), db=Depends(get_db)):
        r = owned_receipt(db, user, rid)
        return render(request, "edit.html", {"r": r}, user=user)

    @app.post("/receipts/{rid}/edit")
    def edit(rid: int, request: Request, vendor: str = Form(""), date_: str = Form("", alias="date"),
             amount: str = Form(""), category: str = Form("uncategorized"), notes: str = Form(""),
             csrf: str = Form(""), user: User = Depends(current_user), db=Depends(get_db)):
        check_csrf(request, csrf)
        r = owned_receipt(db, user, rid)
        r.vendor = vendor.strip()[:255]
        try:
            r.date = datetime.strptime(date_, "%Y-%m-%d").date() if date_ else None
        except ValueError:
            raise HTTPException(400, "Invalid date")
        r.amount_cents = parse_money(amount)
        if r.amount_cents < 0:
            raise HTTPException(400, "Amount cannot be negative")
        r.category = category if category in cats.BY_KEY else "uncategorized"
        r.notes = notes.strip()[:2000]
        r.needs_review = 0 if r.category != "uncategorized" else 1
        db.commit()
        flash(request, "Receipt saved.", "success")
        return RedirectResponse("/receipts", status_code=303)

    @app.post("/receipts/{rid}/delete")
    def delete(rid: int, request: Request, csrf: str = Form(""), user: User = Depends(current_user),
               db=Depends(get_db)):
        check_csrf(request, csrf)
        r = owned_receipt(db, user, rid)
        if r.stored_name:
            (user_dir(user) / r.stored_name).unlink(missing_ok=True)
        db.delete(r)
        db.commit()
        flash(request, "Receipt deleted.", "info")
        return RedirectResponse("/receipts", status_code=303)

    @app.get("/receipts/{rid}/file")
    def receipt_file(rid: int, user: User = Depends(current_user), db=Depends(get_db)):
        r = owned_receipt(db, user, rid)
        if not r.stored_name:
            raise HTTPException(404, "No file")
        path = (user_dir(user) / r.stored_name).resolve()
        if not path.is_file() or user_dir(user).resolve() not in path.parents:
            raise HTTPException(404, "No file")
        return FileResponse(path, media_type=r.content_type, headers={"Cache-Control": "private, no-store"})

    @app.get("/suggest-category")
    def suggest(vendor: str = "", user: User = Depends(current_user)):
        return {"category": cats.suggest_category(vendor)}

    # ------------------------------------------------------------ exports
    @app.get("/export.csv")
    def export_csv(year: str | None = None, user: User = Depends(current_user), db=Depends(get_db)):
        y = parse_year(year)
        body = to_csv(query_receipts(db, user, y, None), y)
        return Response(body, media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="receipts-{y or "all"}.csv"'})

    @app.get("/export.pdf")
    def export_pdf(year: str | None = None, user: User = Depends(current_user), db=Depends(get_db)):
        y = parse_year(year)
        body = to_pdf(query_receipts(db, user, y, None), y, user.email)
        return Response(body, media_type="application/pdf", headers={
            "Content-Disposition": f'attachment; filename="tax-summary-{y or "all"}.pdf"'})

    # ------------------------------------------------------------ plans
    @app.get("/upgrade", response_class=HTMLResponse)
    def upgrade(request: Request, db=Depends(get_db)):
        """Describes Pro. Billing isn't built: there is no payment code and nothing can be bought here."""
        uid = request.session.get("uid")
        user = db.get(User, uid) if uid else None
        return render(request, "upgrade.html", {
            "usage": plans.usage(user, settings) if user else None, "free_limit": settings.free_monthly_receipt_limit,
            "monthly": plans.PRO_PRICE_MONTHLY, "yearly": plans.PRO_PRICE_YEARLY}, user=user)

    # ------------------------------------------------------------ account / privacy
    @app.get("/account", response_class=HTMLResponse)
    def account(request: Request, user: User = Depends(current_user), db=Depends(get_db)):
        n = db.scalar(select(func.count()).select_from(Receipt).where(Receipt.user_id == user.id))
        return render(request, "account.html", {"count": n, "settings": settings,
                                                "usage": plans.usage(user, settings),
                                                "inbound_address": f"r-{user.inbound_alias}@{settings.inbound_email_domain}"},
                      user=user)

    @app.post("/account/delete-data")
    def delete_data(request: Request, csrf: str = Form(""), user: User = Depends(current_user), db=Depends(get_db)):
        check_csrf(request, csrf)
        for r in list(user.receipts):
            db.delete(r)
        db.commit()
        shutil.rmtree(settings.upload_dir / str(user.id), ignore_errors=True)
        flash(request, "All your receipts and files were permanently deleted.", "info")
        return RedirectResponse("/account", status_code=303)

    @app.post("/account/delete")
    def delete_account(request: Request, password: str = Form(...), csrf: str = Form(""),
                       user: User = Depends(current_user), db=Depends(get_db)):
        check_csrf(request, csrf)
        if not check_pw(password, user.password_hash):
            flash(request, "Password incorrect; account not deleted.", "error")
            return RedirectResponse("/account", status_code=303)
        uid = user.id
        db.delete(user)  # cascades to receipts
        db.commit()
        shutil.rmtree(settings.upload_dir / str(uid), ignore_errors=True)
        request.session.clear()
        return RedirectResponse("/?deleted=1", status_code=303)

    # ------------------------------------------------------------ inbound email (stub)
    @app.post("/api/inbound-email")
    async def inbound_email(request: Request, x_inbound_token: str | None = Header(default=None),
                            db=Depends(get_db)):
        """STUB webhook for forwarded receipts. Not connected to any email provider.

        Disabled (503) unless INBOUND_EMAIL_TOKEN is set. Expects JSON:
        {"to": "r-<alias>@<domain>", "from": "...", "subject": "...",
         "attachments": [{"filename": "...", "content_base64": "..."}]}
        A real provider (Postmark/SendGrid/Mailgun inbound parse) would need an adapter to this shape.
        """
        token = settings.inbound_email_token
        if not token:
            return JSONResponse({"error": "inbound email not configured"}, status_code=503)
        if not x_inbound_token or not hmac.compare_digest(x_inbound_token, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        payload = await request.json()
        to = str(payload.get("to", "")).lower()
        local = to.split("@", 1)[0]
        alias = local[2:] if local.startswith("r-") else ""
        user = db.scalar(select(User).where(User.inbound_alias == alias)) if alias else None
        if not user:
            return JSONResponse({"error": "unknown recipient"}, status_code=404)
        ids, skipped = [], 0
        for att in (payload.get("attachments") or [])[:10]:
            try:
                data = base64.b64decode(att.get("content_base64", ""), validate=True)
                ids.append(create_receipt_from_file(db, user, data, att.get("filename"), "email+").id)
            except (binascii.Error, HTTPException) as e:
                log.info("skipping inbound attachment: %s", e)
            except plans.QuotaExceeded:
                skipped += 1
        return {"created": ids, "skipped_over_limit": skipped}

    return app


app = create_app()
