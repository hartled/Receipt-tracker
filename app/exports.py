from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime, timezone

from . import categories as cats

DISCLAIMER = ("General guidance only, not tax advice. Category and deductible-percentage suggestions are "
              "simplified defaults; confirm with a qualified tax professional before filing.")


def summarize(receipts) -> dict:
    by_cat: dict[str, dict] = defaultdict(lambda: {"count": 0, "total": 0, "deductible": 0})
    for r in receipts:
        row = by_cat[r.category]
        row["count"] += 1
        row["total"] += r.amount_cents or 0
        row["deductible"] += cats.deductible_cents(r.amount_cents, r.category)
    rows = []
    for c in cats.CATEGORIES:
        if c.key in by_cat:
            rows.append({"category": c, **by_cat[c.key]})
    return {
        "rows": rows,
        "total": sum(r["total"] for r in rows),
        "deductible": sum(r["deductible"] for r in rows),
        "count": sum(r["count"] for r in rows),
        "needs_review": sum(1 for r in receipts if r.needs_review or r.category == "uncategorized"),
    }


def _money(cents: int) -> str:
    return f"{(cents or 0) / 100:.2f}"


def to_csv(receipts, year: int | None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "vendor", "category", "schedule_c_line", "amount_usd", "deductible_pct",
                "est_deductible_usd", "notes", "receipt_id"])
    for r in receipts:
        c = cats.get(r.category)
        w.writerow([r.date.isoformat() if r.date else "", r.vendor, c.label, c.schedule_c_line,
                    _money(r.amount_cents), c.deductible_pct, _money(cats.deductible_cents(r.amount_cents, r.category)),
                    r.notes, r.id])
    w.writerow([])
    w.writerow([f"Receipt Tracker export for tax year {year or 'all'}; generated "
                f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"])
    w.writerow([DISCLAIMER])
    return buf.getvalue()


def to_pdf(receipts, year: int | None, email: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    s = summarize(receipts)
    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title=f"Tax summary {year or 'all years'}")
    story = [
        Paragraph(f"Expense summary — tax year {year or 'all years'}", styles["Title"]),
        Paragraph(f"Prepared for {email} · generated {datetime.now(timezone.utc):%Y-%m-%d} UTC", styles["Normal"]),
        Spacer(1, 8),
        Paragraph(f"<b>Disclaimer:</b> {DISCLAIMER}", styles["Italic"]),
        Spacer(1, 12),
        Paragraph("Totals by Schedule C-style category", styles["Heading2"]),
    ]
    tstyle = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f6f5c")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f7f6")]),
    ])
    data = [["Category", "Sched. C line", "Receipts", "Total ($)", "Ded. %", "Est. deductible ($)"]]
    for r in s["rows"]:
        c = r["category"]
        data.append([c.label, c.schedule_c_line, r["count"], _money(r["total"]), f"{c.deductible_pct}%",
                     _money(r["deductible"])])
    data.append(["Total", "", s["count"], _money(s["total"]), "", _money(s["deductible"])])
    t = Table(data, repeatRows=1, colWidths=[2.3 * inch, 1.1 * inch, 0.7 * inch, 1 * inch, 0.6 * inch, 1.3 * inch])
    t.setStyle(tstyle)
    t.setStyle(TableStyle([("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]))
    story += [t, Spacer(1, 6)]
    if s["needs_review"]:
        story.append(Paragraph(f"{s['needs_review']} receipt(s) still need review/categorization.", styles["Normal"]))
    story += [Spacer(1, 12), Paragraph("Receipt detail", styles["Heading2"])]
    detail = [["Date", "Vendor", "Category", "Amount ($)", "Notes"]]
    for r in receipts:
        detail.append([r.date.isoformat() if r.date else "—", Paragraph(r.vendor or "—", styles["BodyText"]),
                       cats.get(r.category).label, _money(r.amount_cents),
                       Paragraph((r.notes or "")[:200], styles["BodyText"])])
    t2 = Table(detail, repeatRows=1, colWidths=[0.85 * inch, 1.9 * inch, 1.8 * inch, 0.85 * inch, 1.6 * inch])
    t2.setStyle(tstyle)
    t2.setStyle(TableStyle([("ALIGN", (2, 0), (2, -1), "LEFT"), ("ALIGN", (4, 0), (4, -1), "LEFT"),
                            ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(t2)

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.drawString(0.6 * inch, 0.35 * inch, "General guidance only, not tax advice. — Receipt Tracker")
        canvas.drawRightString(letter[0] - 0.6 * inch, 0.35 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
