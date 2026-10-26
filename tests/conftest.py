import io
import os
import re
import tempfile

# Ensure importing app.main (which builds a default app) never touches ./data.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="rt-test-"))

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, secret_key="test-secret", extractor="heuristic",
                    inbound_email_token="inbound-secret")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def csrf_of(client, path="/login"):
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def signup(client, email="alice@example.com", password="correct-horse"):
    tok = csrf_of(client, "/signup")
    r = client.post("/signup", data={"email": email, "password": password, "csrf": tok}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return csrf_of(client, "/receipts/new")


def make_pdf(lines):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 720
    for line in lines:
        c.drawString(72, y, line)
        y -= 18
    c.save()
    return buf.getvalue()


def make_png(lines):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 28)
    except OSError:
        font = ImageFont.load_default(size=28)
    img = Image.new("RGB", (700, 60 + 44 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((30, 30 + 44 * i), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
