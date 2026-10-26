from datetime import date

import pytest

from app import categories as cats
from app.extraction import HeuristicExtractor, parse_amount, parse_date, parse_text, tesseract_available
from tests.conftest import make_png


def test_parse_total_prefers_total_line_over_subtotal_and_tax():
    text = "Blue Bottle Coffee\n03/14/2026\nLatte 5.50\nSubtotal 12.00\nTax 1.07\nTOTAL $13.07\nCash 20.00\nChange 6.93"
    r = parse_text(text, "test")
    assert r.vendor == "Blue Bottle Coffee"
    assert r.date == date(2026, 3, 14)
    assert r.amount_cents == 1307


def test_parse_date_formats():
    assert parse_date("2026-01-05") == date(2026, 1, 5)
    assert parse_date("Date: Jan 5, 2026") == date(2026, 1, 5)
    assert parse_date("5 March 2026") == date(2026, 3, 5)
    assert parse_date("12/31/25") == date(2025, 12, 31)
    assert parse_date("no date here") is None


def test_parse_amount_fallback_and_commas():
    assert parse_amount("Item 1,234.50\nItem 3.00") == 123450
    assert parse_amount("nothing") is None


@pytest.mark.parametrize("vendor,expected", [
    ("Starbucks #123", "meals"), ("Uber Eats", "meals"), ("Uber", "car_truck"), ("Adobe Inc", "software"),
    ("Delta Air Lines", "travel"), ("Staples", "office_expense"), ("Random Shop", "uncategorized"),
])
def test_category_suggestion(vendor, expected):
    assert cats.suggest_category(vendor) == expected


def test_deductible_meals_50pct():
    assert cats.deductible_cents(1000, "meals") == 500
    assert cats.deductible_cents(1000, "software") == 1000
    assert cats.deductible_cents(1000, "personal") == 0


@pytest.mark.skipif(not tesseract_available(), reason="tesseract not installed")
def test_ocr_image(tmp_path):
    p = tmp_path / "r.png"
    p.write_bytes(make_png(["OFFICE DEPOT", "Date 04/02/2026", "Paper 9.99", "Subtotal 9.99", "TOTAL 10.79"]))
    r = HeuristicExtractor().extract(p, "image/png")
    assert r.source == "tesseract"
    assert r.amount_cents == 1079
    assert r.date == date(2026, 4, 2)
    assert cats.suggest_category(r.vendor, r.raw_text) == "office_expense"
