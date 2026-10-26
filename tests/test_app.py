import base64

from tests.conftest import csrf_of, make_pdf, signup


def upload(client, tok, data, name="r.pdf", ctype="application/pdf"):
    return client.post("/receipts/new", data={"csrf": tok}, files=[("files", (name, data, ctype))],
                       follow_redirects=False)


def test_pages_require_login(client):
    for path in ["/dashboard", "/receipts", "/receipts/new", "/export.csv", "/account"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/privacy").status_code == 200
    assert client.get("/healthz").json() == {"ok": True}


def test_signup_login_logout_and_password_hashed(client, settings):
    tok = signup(client)
    assert "Dashboard" in client.get("/dashboard").text
    client.post("/logout", data={"csrf": tok})
    assert client.get("/dashboard", follow_redirects=False).status_code == 303
    t = csrf_of(client)
    bad = client.post("/login", data={"email": "alice@example.com", "password": "nope", "csrf": t})
    assert bad.status_code == 400
    ok = client.post("/login", data={"email": "alice@example.com", "password": "correct-horse", "csrf": t},
                     follow_redirects=False)
    assert ok.status_code == 303
    import sqlite3
    (h,) = sqlite3.connect(settings.data_dir / "receipts.db").execute("select password_hash from users").fetchone()
    assert h.startswith("$2b$") and "correct-horse" not in h


def test_csrf_required(client):
    signup(client)
    r = client.post("/receipts/new", data={"csrf": "wrong"})
    assert r.status_code == 400


def test_upload_pdf_extract_edit_filter_export_delete(client):
    tok = signup(client)
    pdf = make_pdf(["Adobe Inc", "Invoice date: 2026-02-10", "Creative Cloud 54.99", "Tax 4.40", "Total $59.39"])
    r = upload(client, tok, pdf)
    assert r.status_code == 303
    edit_url = r.headers["location"]
    page = client.get(edit_url).text
    assert 'value="Adobe Inc"' in page and 'value="2026-02-10"' in page and 'value="59.39"' in page
    assert '<option value="software" selected' in page

    # manual meal receipt
    r = client.post("/receipts/new", data={"csrf": tok}, follow_redirects=False)
    meal_url = r.headers["location"]
    client.post(meal_url, data={"csrf": tok, "vendor": "Client lunch", "date": "2026-03-01", "amount": "80.00",
                                "category": "meals", "notes": "Project kickoff"})
    # confirm adobe
    client.post(edit_url, data={"csrf": tok, "vendor": "Adobe Inc", "date": "2026-02-10", "amount": "59.39",
                                "category": "software", "notes": ""})

    dash = client.get("/dashboard?year=2026").text
    assert "$139.39" in dash  # total
    assert "$99.39" in dash   # 59.39 + 50% of 80

    lst = client.get("/receipts?year=2026&category=meals").text
    assert "Client lunch" in lst and "Adobe Inc" not in lst
    assert "Client lunch" not in client.get("/receipts?year=2025").text

    csv_ = client.get("/export.csv?year=2026")
    assert csv_.headers["content-type"].startswith("text/csv")
    assert "Adobe Inc" in csv_.text and "not tax advice" in csv_.text and "Line 24b" in csv_.text

    pdf_out = client.get("/export.pdf?year=2026")
    assert pdf_out.content.startswith(b"%PDF")
    from io import BytesIO
    from pypdf import PdfReader
    text = "".join(p.extract_text() for p in PdfReader(BytesIO(pdf_out.content)).pages)
    assert "not tax advice" in text and "Business meals" in text

    rid = edit_url.split("/")[2]
    assert client.get(f"/receipts/{rid}/file").content.startswith(b"%PDF")
    client.post(f"/receipts/{rid}/delete", data={"csrf": tok})
    assert client.get(edit_url).status_code == 404


def test_rejects_non_receipt_files(client):
    tok = signup(client)
    r = upload(client, tok, b"#!/bin/sh\necho hi", name="x.sh", ctype="text/plain")
    assert r.status_code == 415


def test_users_are_isolated(client, settings):
    tok = signup(client, "a@example.com")
    r = upload(client, tok, make_pdf(["Staples", "2026-01-01", "Total 10.00"]))
    edit_url = r.headers["location"]
    rid = edit_url.split("/")[2]
    client.post("/logout", data={"csrf": tok})
    tok2 = signup(client, "b@example.com")
    assert client.get(edit_url).status_code == 404
    assert client.get(f"/receipts/{rid}/file").status_code == 404
    assert client.post(f"/receipts/{rid}/delete", data={"csrf": tok2}).status_code == 404
    assert "Staples" not in client.get("/receipts").text


def test_delete_data_and_account(client, settings):
    tok = signup(client)
    upload(client, tok, make_pdf(["Staples", "2026-01-01", "Total 10.00"]))
    user_dir = next((settings.upload_dir).iterdir())
    assert any(user_dir.iterdir())
    client.post("/account/delete-data", data={"csrf": tok})
    assert not user_dir.exists()
    assert "0 receipt(s)" in client.get("/account").text
    r = client.post("/account/delete", data={"csrf": tok, "password": "wrong"}, follow_redirects=False)
    assert client.get("/account").status_code == 200  # still logged in
    client.post("/account/delete", data={"csrf": tok, "password": "correct-horse"})
    t = csrf_of(client)
    r = client.post("/login", data={"email": "alice@example.com", "password": "correct-horse", "csrf": t})
    assert r.status_code == 400


def test_inbound_email_stub(client, settings):
    tok = signup(client)
    acct = client.get("/account").text
    import re
    addr = re.search(r"<code>([^<]+)</code>", acct).group(1)
    payload = {"to": addr, "from": "alice@example.com", "subject": "Fwd: receipt",
               "attachments": [{"filename": "r.pdf", "content_base64": base64.b64encode(
                   make_pdf(["Zoom Video", "2026-05-05", "Total 15.99"])).decode()}]}
    assert client.post("/api/inbound-email", json=payload).status_code == 401
    r = client.post("/api/inbound-email", json=payload, headers={"X-Inbound-Token": "inbound-secret"})
    assert r.status_code == 200 and len(r.json()["created"]) == 1
    assert "Zoom Video" in client.get("/receipts").text
    payload["to"] = "r-unknown@in.example.com"
    assert client.post("/api/inbound-email", json=payload,
                       headers={"X-Inbound-Token": "inbound-secret"}).status_code == 404


def test_inbound_disabled_without_token(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    s = Settings(data_dir=tmp_path, secret_key="x", inbound_email_token=None)
    s.inbound_email_token = None
    with TestClient(create_app(s)) as c:
        assert c.post("/api/inbound-email", json={}).status_code == 503
