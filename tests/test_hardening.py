"""Login rate limiting, password reset, and the free-tier monthly limit."""
import logging
import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import csrf_of, make_pdf, signup

PW = "correct-horse"


def make_client(tmp_path, **kw):
    base = dict(data_dir=tmp_path, secret_key="test-secret", extractor="heuristic", inbound_email_token="inbound-secret")
    base.update(kw)
    return TestClient(create_app(Settings(**base)))


def login(c, email, password, headers=None):
    return c.post("/login", data={"email": email, "password": password, "csrf": csrf_of(c)},
                  headers=headers or {}, follow_redirects=False)


def db(tmp_path):
    return sqlite3.connect(tmp_path / "receipts.db")


# ------------------------------------------------------------------ rate limiting
def test_lockout_per_email_even_with_right_password(tmp_path):
    with make_client(tmp_path, login_max_failures_per_email=3, login_max_failures_per_ip=100) as c:
        signup(c)
        c.cookies.clear()
        for _ in range(3):
            assert login(c, "alice@example.com", "wrong").status_code == 400
        r = login(c, "alice@example.com", PW)
        assert r.status_code == 429 and "Too many failed login attempts" in r.text and int(r.headers["Retry-After"]) > 0
        # Unknown emails lock the same way, so lockout doesn't reveal which accounts exist.
        for _ in range(3):
            login(c, "nobody@example.com", "wrong")
        assert login(c, "nobody@example.com", "x").status_code == 429
        # Other accounts from the same IP are unaffected by a per-email lock.
        signup(c, "bob@example.com")
        c.cookies.clear()
        assert login(c, "bob@example.com", PW).status_code == 303


def test_lockout_per_ip_and_forwarded_for(tmp_path):
    with make_client(tmp_path, login_max_failures_per_email=100, login_max_failures_per_ip=3,
                     trusted_proxy_hops=1) as c:
        signup(c)
        c.cookies.clear()
        a = {"X-Forwarded-For": "203.0.113.5"}
        for i in range(3):
            login(c, f"user{i}@example.com", "wrong", a)
        assert login(c, "alice@example.com", PW, a).status_code == 429
        # A different client IP (as reported by the trusted proxy) is not blocked...
        assert login(c, "alice@example.com", PW, {"X-Forwarded-For": "198.51.100.7"}).status_code == 303
        c.cookies.clear()
        # ...and a forged left-most entry doesn't help the blocked client escape.
        assert login(c, "alice@example.com", PW, {"X-Forwarded-For": "1.2.3.4, 203.0.113.5"}).status_code == 429


def test_forwarded_for_ignored_without_trusted_proxy(tmp_path):
    with make_client(tmp_path, login_max_failures_per_email=100, login_max_failures_per_ip=2) as c:
        signup(c)
        c.cookies.clear()
        login(c, "x@example.com", "wrong", {"X-Forwarded-For": "1.1.1.1"})
        login(c, "y@example.com", "wrong", {"X-Forwarded-For": "2.2.2.2"})
        assert login(c, "alice@example.com", PW, {"X-Forwarded-For": "3.3.3.3"}).status_code == 429


def test_lockout_expires_and_success_resets_counter(tmp_path):
    with make_client(tmp_path, login_max_failures_per_email=2, login_window_seconds=1,
                     login_lockout_seconds=1) as c:
        signup(c)
        c.cookies.clear()
        login(c, "alice@example.com", "wrong")
        login(c, "alice@example.com", "wrong")
        assert login(c, "alice@example.com", PW).status_code == 429
        time.sleep(1.3)
        assert login(c, "alice@example.com", PW).status_code == 303
    with make_client(tmp_path / "b", login_max_failures_per_email=2) as c:
        signup(c)
        c.cookies.clear()
        login(c, "alice@example.com", "wrong")
        assert login(c, "alice@example.com", PW).status_code == 303  # clears the per-email counter
        c.cookies.clear()
        login(c, "alice@example.com", "wrong")
        assert login(c, "alice@example.com", PW).status_code == 303


# ------------------------------------------------------------------ password reset
def request_reset(c, email):
    return c.post("/forgot-password", data={"email": email, "csrf": csrf_of(c, "/forgot-password")})


def link_from_logs(caplog):
    m = re.search(r"(http\S+/reset-password\?token=(\S+))", caplog.text)
    return m.group(1), m.group(2)


def test_reset_does_not_leak_and_logs_only_in_dev_mode(tmp_path, caplog):
    with make_client(tmp_path, dev_mode=False) as c:
        signup(c)
        c.cookies.clear()
        with caplog.at_level(logging.INFO, logger="receipt_tracker"):
            known, unknown = request_reset(c, "alice@example.com"), request_reset(c, "ghost@example.com")
        strip = lambda html: re.sub(r'name="csrf" value="[^"]+"', "", html)
        assert known.status_code == unknown.status_code == 200
        assert strip(known.text) == strip(unknown.text)
        assert "If an account exists" in known.text and "token=" not in known.text
        assert "token=" not in caplog.text and "no email provider is configured" in caplog.text
        (h,) = db(tmp_path).execute("select token_hash from password_reset_tokens").fetchone()
        assert len(h) == 64  # a token was created, but only its hash is stored

    with make_client(tmp_path / "dev", dev_mode=True, public_base_url="http://localhost:8000") as c:
        signup(c)
        c.cookies.clear()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="receipt_tracker"):
            r = request_reset(c, "alice@example.com")
        link, raw = link_from_logs(caplog)
        assert link.startswith("http://localhost:8000/reset-password?token=")
        assert raw not in r.text
        rows = db(tmp_path / "dev").execute("select token_hash from password_reset_tokens").fetchall()
        assert all(raw not in h for (h,) in rows)


def test_reset_flow_single_use_and_logs_out_other_sessions(tmp_path, caplog):
    with make_client(tmp_path, dev_mode=True) as c, \
         TestClient(c.app) as other:
        signup(c)
        assert login(other, "alice@example.com", PW).status_code == 303  # second, separate session
        assert other.get("/dashboard", follow_redirects=False).status_code == 200
        c.cookies.clear()
        with caplog.at_level(logging.WARNING, logger="receipt_tracker"):
            request_reset(c, "alice@example.com")
        _, raw = link_from_logs(caplog)
        form = c.get(f"/reset-password?token={raw}")
        assert form.status_code == 200 and 'name="token"' in form.text
        tok = csrf_of(c, f"/reset-password?token={raw}")
        bad = c.post("/reset-password", data={"token": raw, "password": "newpass123", "confirm": "nope1234",
                                              "csrf": tok})
        assert bad.status_code == 400 and "match" in bad.text
        ok = c.post("/reset-password", data={"token": raw, "password": "newpass123", "confirm": "newpass123",
                                             "csrf": tok}, follow_redirects=False)
        assert ok.status_code == 303 and ok.headers["location"] == "/login"
        # single use
        again = c.post("/reset-password", data={"token": raw, "password": "another123", "confirm": "another123",
                                                "csrf": csrf_of(c)})
        assert again.status_code == 400 and "invalid, expired" in again.text
        assert c.get(f"/reset-password?token={raw}").status_code == 400
        # old password fails, new works, other session was logged out
        assert login(c, "alice@example.com", PW).status_code == 400
        assert login(c, "alice@example.com", "newpass123").status_code == 303
        assert other.get("/dashboard", follow_redirects=False).headers["location"] == "/login"


def test_reset_token_expiry_and_revocation(tmp_path, caplog):
    with make_client(tmp_path, dev_mode=True) as c:
        signup(c)
        c.cookies.clear()
        with caplog.at_level(logging.WARNING, logger="receipt_tracker"):
            request_reset(c, "alice@example.com")
            _, first = link_from_logs(caplog)
            caplog.clear()
            request_reset(c, "alice@example.com")
            _, second = link_from_logs(caplog)
        assert first != second
        assert c.get(f"/reset-password?token={first}").status_code == 400   # revoked by the newer link
        assert c.get(f"/reset-password?token={second}").status_code == 200
        con = db(tmp_path)
        con.execute("update password_reset_tokens set expires_at = '2000-01-01 00:00:00'")
        con.commit()
        assert c.get(f"/reset-password?token={second}").status_code == 400
        assert c.get("/reset-password?token=garbage").status_code == 400


def test_reset_requests_are_throttled_silently(tmp_path, caplog):
    with make_client(tmp_path, dev_mode=True, reset_max_requests_per_email=2) as c:
        signup(c)
        c.cookies.clear()
        with caplog.at_level(logging.WARNING, logger="receipt_tracker"):
            for _ in range(4):
                assert request_reset(c, "alice@example.com").status_code == 200
        assert caplog.text.count("reset-password?token=") == 2


# ------------------------------------------------------------------ free-tier limit
def upload(c, tok, n=1):
    files = [("files", (f"r{i}.pdf", make_pdf(["Staples", "2026-01-01", "Total 10.00"]), "application/pdf"))
             for i in range(n)]
    return c.post("/receipts/new", data={"csrf": tok}, files=files, follow_redirects=False)


def count_receipts(tmp_path):
    return db(tmp_path).execute("select count(*) from receipts").fetchone()[0]


def test_free_limit_enforced_with_friendly_message(tmp_path):
    with make_client(tmp_path, free_monthly_receipt_limit=3) as c:
        tok = signup(c)
        assert "0 of 3 receipts used" in c.get("/dashboard").text
        assert upload(c, tok, 2).status_code == 303
        r = upload(c, tok, 2)  # only one slot left: partial upload
        assert r.headers["location"] == "/receipts" and count_receipts(tmp_path) == 3
        assert "Skipped 1 file(s)" in c.get("/receipts").text
        r = upload(c, tok)
        assert r.status_code == 303 and r.headers["location"] == "/receipts/new"
        page = c.get("/receipts/new").text
        assert "used all 3 free receipts" in page and "/upgrade" in page
        # manual entry counts too
        assert c.post("/receipts/new", data={"csrf": tok}, follow_redirects=False).headers["location"] == "/receipts/new"
        assert count_receipts(tmp_path) == 3
        dash = c.get("/dashboard").text
        assert "3 of 3 receipts used" in dash and "reached this month" in dash
        # deleting doesn't free a slot
        (rid,) = db(tmp_path).execute("select id from receipts limit 1").fetchone()
        c.post(f"/receipts/{rid}/delete", data={"csrf": tok})
        assert upload(c, tok).headers["location"] == "/receipts/new"
        # a new month resets the counter
        con = db(tmp_path)
        con.execute("update users set usage_month = '2000-01'")
        con.commit()
        assert upload(c, tok).headers["location"].endswith("/edit")
        assert "1 of 3 receipts used" in c.get("/dashboard").text


def test_inbound_email_respects_limit(tmp_path):
    import base64
    with make_client(tmp_path, free_monthly_receipt_limit=1) as c:
        signup(c)
        addr = re.search(r"<code>([^<]+)</code>", c.get("/account").text).group(1)
        att = {"filename": "r.pdf", "content_base64": base64.b64encode(make_pdf(["Zoom", "Total 1.00"])).decode()}
        r = c.post("/api/inbound-email", json={"to": addr, "attachments": [att, att]},
                   headers={"X-Inbound-Token": "inbound-secret"})
        assert len(r.json()["created"]) == 1 and r.json()["skipped_over_limit"] == 1


def test_cli_marks_user_pro_unlimited(tmp_path, monkeypatch, capsys):
    from app import cli
    with make_client(tmp_path, free_monthly_receipt_limit=1) as c:
        tok = signup(c)
        upload(c, tok)
        assert upload(c, tok).headers["location"] == "/receipts/new"
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert cli.main(["set-plan", "Alice@Example.com", "pro"]) == 0
        assert "now on the Pro plan" in capsys.readouterr().out
        assert cli.main(["show", "nobody@example.com"]) == 1
        for _ in range(3):
            assert upload(c, tok).headers["location"].endswith("/edit")
        dash = c.get("/dashboard").text
        assert "Pro plan" in dash and "unlimited" in dash
        assert ">Upgrade<" not in c.get("/dashboard").text  # nav link hidden for Pro
        assert cli.main(["list"]) == 0 and "alice@example.com\tpro" in capsys.readouterr().out


def test_upgrade_page_has_prices_and_no_billing(tmp_path):
    with make_client(tmp_path) as c:
        anon = c.get("/upgrade")
        assert anon.status_code == 200
        signup(c)
        page = c.get("/upgrade").text
        assert "$6" in page and "$49" in page and "Billing isn't available yet" in page
        assert "0 of 15 free receipts used" in page
        low = page.lower()
        assert "stripe" not in low and "card number" not in low and 'action="/upgrade' not in low
        assert c.post("/upgrade").status_code == 405


def test_new_user_defaults_to_free_plan(tmp_path):
    with make_client(tmp_path) as c:
        signup(c)
    assert db(tmp_path).execute("select plan from users").fetchone() == ("free",)


def test_migrates_existing_database(tmp_path):
    con = db(tmp_path)
    con.execute("create table users (id integer primary key, email varchar(255) unique, password_hash varchar(255),"
                " inbound_alias varchar(64) unique, created_at datetime)")
    con.execute("insert into users (email, password_hash, inbound_alias) values ('old@example.com', 'x', 'abc')")
    con.commit()
    with make_client(tmp_path):
        pass
    cols = {r[1] for r in db(tmp_path).execute("pragma table_info(users)")}
    assert {"plan", "session_epoch", "usage_month", "usage_count"} <= cols
    assert db(tmp_path).execute("select plan, usage_count from users").fetchone() == ("free", 0)


def test_access_log_redacts_reset_tokens():
    from app.main import RedactTokens
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', 
                            ("1.2.3.4", "GET", "/reset-password?token=SECRET123&x=1", "1.1", 200), None)
    RedactTokens().filter(rec)
    assert "SECRET123" not in rec.getMessage() and "token=[redacted]&x=1" in rec.getMessage()
