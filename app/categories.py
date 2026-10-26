"""IRS Schedule C-style categories and rule-based suggestion.

General guidance only, not tax advice. Deductible percentages are simplified
defaults (e.g. business meals generally 50%) and the user must confirm.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    schedule_c_line: str
    deductible_pct: int  # simplified default
    note: str = ""
    keywords: tuple[str, ...] = ()


CATEGORIES: list[Category] = [
    Category("advertising", "Advertising & marketing", "Line 8", 100, "",
             ("facebook ads", "google ads", "meta ads", "linkedin ads", "vistaprint", "mailchimp", "canva", "advertis")),
    Category("car_truck", "Car & truck expenses", "Line 9", 100,
             "Business-use portion only; standard mileage vs actual expenses.",
             ("shell", "chevron", "exxon", "mobil", "bp ", "sunoco", "gas station", "fuel", "parking", "toll", "uber", "lyft", "jiffy lube")),
    Category("commissions_fees", "Commissions & fees", "Line 10", 100, "",
             ("upwork", "fiverr", "stripe fee", "paypal fee", "etsy fee", "commission")),
    Category("contract_labor", "Contract labor", "Line 11", 100, "1099-NEC may be required for payees.",
             ("contractor", "freelancer payment", "subcontract")),
    Category("equipment", "Equipment (depreciation / Sec. 179)", "Line 13", 100,
             "Larger purchases may need to be depreciated; ask a tax pro.",
             ("apple store", "best buy", "b&h", "dell", "lenovo", "laptop", "monitor", "camera")),
    Category("insurance", "Insurance (non-health)", "Line 15", 100, "",
             ("insurance", "hiscox", "next insurance", "geico business")),
    Category("legal_professional", "Legal & professional services", "Line 17", 100, "",
             ("attorney", "law office", "legal", "cpa", "accountant", "bookkeep", "legalzoom")),
    Category("office_expense", "Office expense", "Line 18", 100, "",
             ("staples", "office depot", "officemax", "usps", "fedex", "ups store", "postage", "printer", "ink")),
    Category("rent_lease", "Rent or lease (coworking, equipment)", "Line 20", 100, "",
             ("wework", "regus", "coworking", "rent", "lease")),
    Category("repairs", "Repairs & maintenance", "Line 21", 100, "",
             ("repair", "maintenance", "geek squad")),
    Category("supplies", "Supplies", "Line 22", 100, "",
             ("home depot", "lowe's", "lowes", "michaels", "supplies", "amazon")),
    Category("taxes_licenses", "Taxes & licenses", "Line 23", 100, "",
             ("license", "permit", "registration fee", "secretary of state")),
    Category("travel", "Travel (lodging, airfare)", "Line 24a", 100, "Business travel away from tax home.",
             ("airlines", "airline", "delta", "united", "american air", "southwest", "jetblue", "hotel", "marriott",
              "hilton", "hyatt", "airbnb", "expedia", "amtrak")),
    Category("meals", "Business meals", "Line 24b", 50, "Generally 50% deductible; keep note of business purpose.",
             ("restaurant", "cafe", "coffee", "starbucks", "grill", "pizza", "bistro", "diner", "kitchen", "bar ",
              "doordash", "grubhub", "uber eats", "chipotle", "sweetgreen")),
    Category("utilities", "Utilities, phone & internet", "Line 25", 100, "Business-use portion only.",
             ("verizon", "at&t", "t-mobile", "comcast", "xfinity", "spectrum", "internet", "electric", "utility")),
    Category("software", "Software & subscriptions", "Line 27a (other)", 100, "",
             ("adobe", "github", "google workspace", "microsoft", "notion", "slack", "zoom", "dropbox", "figma",
              "aws", "digitalocean", "heroku", "render", "openai", "subscription", "saas", "jetbrains")),
    Category("education", "Education & training", "Line 27a (other)", 100, "Must maintain/improve current skills.",
             ("udemy", "coursera", "conference", "workshop", "course", "book", "o'reilly")),
    Category("bank_fees", "Bank & payment fees", "Line 27a (other)", 100, "",
             ("bank fee", "service charge", "wire fee", "overdraft")),
    Category("home_office", "Home office (Form 8829)", "Line 30", 100,
             "Simplified or regular method; calculated separately.", ()),
    Category("personal", "Personal (not deductible)", "-", 0, "", ()),
    Category("uncategorized", "Uncategorized / needs review", "-", 0, "Not counted as deductible until categorized.", ()),
]

BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}


def get(key: str | None) -> Category:
    return BY_KEY.get(key or "", BY_KEY["uncategorized"])


def suggest_category(vendor: str = "", text: str = "") -> str:
    """Return the best-matching category key. Vendor matches outweigh body-text matches."""
    vendor_l = f" {(vendor or '').lower()} "
    text_l = f" {(text or '').lower()} "
    best, best_score = "uncategorized", 0
    for cat in CATEGORIES:
        score = 0
        for kw in cat.keywords:
            weight = 1 + len(kw) / 20  # longer (more specific) keywords win ties, e.g. "uber eats" > "uber"
            if kw in vendor_l:
                score += 3 * weight
            elif kw in text_l:
                score += weight
        if score > best_score:
            best, best_score = cat.key, score
    return best


def deductible_cents(amount_cents: int, key: str) -> int:
    return round((amount_cents or 0) * get(key).deductible_pct / 100)
