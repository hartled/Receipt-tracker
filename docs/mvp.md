# Receipt Tracker — MVP (one page)

**Who it's for:** US freelancers and sole proprietors who file Schedule C and want receipts organized for tax time with little effort.

## In scope
- Email/password accounts (bcrypt-hashed passwords, signed session cookie).
- Login rate limiting: per-email and per-IP lockout after repeated failures, stored in SQLite and configurable with env vars.
- Password reset: hashed, single-use, expiring tokens. Responses don't reveal whether an account exists. No email provider yet, so in `DEV_MODE` the link goes to the server log only. A reset signs out all sessions.
- Plans: `free` (default) or `pro`. Free = 15 new receipts per calendar month, enforced on upload, manual entry and inbound email. Pro = unlimited. Usage is shown on the dashboard. The `/upgrade` page lists Pro at $6/mo or $49/yr and says billing isn't available yet. Pro is set with the CLI (`python -m app.cli set-plan EMAIL pro`) for testing.
- Upload a receipt image (JPG/PNG) or PDF. Files are stored per user and served only to their owner.
- Extraction runs behind a pluggable interface:
  - Default `heuristic`: free. Runs Tesseract OCR when it's installed, plus PDF text extraction and regex parsing of vendor, date and total.
  - Optional `openai`: used only when `OPENAI_API_KEY` is set. It's off by default and never required.
  - A manual edit form is always available to fix the results.
- Category suggestion from vendor/keyword rules, mapped to IRS Schedule C-style lines. Each category has a deductible % (for example, meals at 50%).
- Receipt list with filters by year and category, plus edit and delete.
- Dashboard: totals and estimated deductible amount by category for the selected year.
- Export: CSV and PDF tax summary with a "general guidance only, not tax advice" disclaimer.
- Privacy: privacy page, "delete all my data", and "delete account".
- Email forwarding: a stub inbound webhook (`POST /api/inbound-email`), documented only. No mail service is connected.
- Deployment: Docker plus `render.yaml`. `DATA_DIR` holds the SQLite DB and uploads.

## Deferred
Real inbound email, mobile app or camera capture, bank/card feeds and matching, mileage, multi-user or accountant sharing, Stripe billing, real email delivery (password reset links and receipt forwarding), email verification, Postgres/S3 storage, line items, multi-currency, quarterly estimated-tax calculator.

## Pricing (decided by D)
- **Free:** 15 receipts per calendar month, with all current features, including CSV and PDF exports.
- **Pro:** $6/mo or $49/yr, with unlimited receipts. The limit is built into the app. Billing isn't built yet, and Stripe needs D's OK.
- This sits under Shoeboxed ($9) and Wave receipts ($8) and is a little below the free caps elsewhere (20–25). An annual "tax season pass" is a possible later option.

## Risks
- **Tax-advice liability.** Mitigation: the disclaimer everywhere, "suggested" wording, and the user confirms each category.
- **OCR accuracy.** The heuristic extractor is basic. Mitigation: a fast edit form and an optional LLM provider (which adds cost per receipt).
- **Sensitive data.** Receipts can contain personal data. Mitigations: per-user storage, HTTPS, deletion, no third-party sharing by default. Turning on an LLM provider sends images to that vendor, and the privacy page needs to say so.
- **Hosting.** SQLite and local files are lost on Render's free tier without a paid persistent disk. We'll need Postgres plus object storage to scale.
- **Competition.** Expensify's free tier is strong. We win only on freelancer tax focus and simplicity.
