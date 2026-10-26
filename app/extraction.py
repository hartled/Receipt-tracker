"""Pluggable receipt extraction.

Providers:
- HeuristicExtractor (default): free + local. PDF text via pypdf, image OCR via
  Tesseract (pytesseract) when the `tesseract` binary is installed, then regex parsing.
- OpenAIExtractor (optional): only used when EXTRACTOR=openai and OPENAI_API_KEY is set.
  Falls back to the heuristic extractor on any error.
- NullExtractor: no extraction; the user fills the manual form.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    vendor: str = ""
    date: date | None = None
    amount_cents: int | None = None
    raw_text: str = ""
    source: str = "none"


class Extractor(Protocol):
    name: str

    def extract(self, path: Path, content_type: str) -> ExtractionResult: ...


# ---------------------------------------------------------------- parsing helpers
_AMOUNT_RE = re.compile(r"(?<![\d.])\$?\s?(\d{1,3}(?:,\d{3})*|\d+)[.,](\d{2})(?!\d)")
_TOTAL_HINT = re.compile(r"\b(grand\s+total|total\s+due|amount\s+due|balance\s+due|total|amount\s+paid|charged)\b", re.I)
_NOT_TOTAL = re.compile(r"\b(sub\s*-?\s*total|tax|tip|change|savings|discount)\b", re.I)
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "mdy"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b"), "mdy2"),
    (re.compile(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b"), "mdy"),
    (re.compile(rf"\b({_MONTHS})[a-z]*\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I), "mon_d_y"),
    (re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})[a-z]*\.?,?\s+(\d{{4}})\b", re.I), "d_mon_y"),
]
_MONTH_NUM = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _to_cents(whole: str, frac: str) -> int:
    return int(whole.replace(",", "")) * 100 + int(frac)


def parse_amount(text: str) -> int | None:
    lines = text.splitlines()
    candidates: list[int] = []
    for i, line in enumerate(lines):
        if _TOTAL_HINT.search(line) and not _NOT_TOTAL.search(line):
            amounts = _AMOUNT_RE.findall(line)
            if not amounts and i + 1 < len(lines):  # amount on the next line
                amounts = _AMOUNT_RE.findall(lines[i + 1])
            if amounts:
                candidates.append(_to_cents(*amounts[-1]))
    if candidates:
        return max(candidates)
    all_amounts = [_to_cents(*m) for m in _AMOUNT_RE.findall(text)]
    return max(all_amounts) if all_amounts else None


def parse_date(text: str) -> date | None:
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            try:
                g = m.groups()
                if kind == "ymd":
                    d = date(int(g[0]), int(g[1]), int(g[2]))
                elif kind == "mdy":
                    d = date(int(g[2]), int(g[0]), int(g[1]))
                elif kind == "mdy2":
                    d = date(2000 + int(g[2]), int(g[0]), int(g[1]))
                elif kind == "mon_d_y":
                    d = date(int(g[2]), _MONTH_NUM[g[0][:3].lower()], int(g[1]))
                else:
                    d = date(int(g[2]), _MONTH_NUM[g[1][:3].lower()], int(g[0]))
            except (ValueError, KeyError):
                continue
            if 2000 <= d.year <= date.today().year + 1:
                return d
    return None


def parse_vendor(text: str) -> str:
    for line in text.splitlines()[:8]:
        clean = re.sub(r"\s+", " ", line).strip(" *#-=:|")
        letters = sum(c.isalpha() for c in clean)
        if letters >= 3 and len(clean) <= 60 and not _AMOUNT_RE.search(clean) and not parse_date(clean) \
                and not re.search(r"\b(receipt|invoice|welcome|thank you|tel|phone|www\.|http)\b", clean, re.I):
            return clean[:120]
    return ""


def parse_text(text: str, source: str) -> ExtractionResult:
    return ExtractionResult(vendor=parse_vendor(text), date=parse_date(text),
                            amount_cents=parse_amount(text), raw_text=text[:20000], source=source)


# ---------------------------------------------------------------- providers
def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


class NullExtractor:
    name = "none"

    def extract(self, path: Path, content_type: str) -> ExtractionResult:
        return ExtractionResult(source="none")


class HeuristicExtractor:
    name = "heuristic"

    def _text(self, path: Path, content_type: str) -> tuple[str, str]:
        if content_type == "application/pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:5])
            return text, "pdf-text"
        if tesseract_available():
            import pytesseract
            from PIL import Image, ImageOps
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im).convert("L")
                if im.width < 1000:  # upscale small photos for better OCR
                    im = im.resize((im.width * 2, im.height * 2))
                return pytesseract.image_to_string(im), "tesseract"
        return "", "none"

    def extract(self, path: Path, content_type: str) -> ExtractionResult:
        try:
            text, src = self._text(path, content_type)
        except Exception:  # never fail an upload because extraction failed
            log.exception("heuristic extraction failed")
            return ExtractionResult(source="error")
        return parse_text(text, src) if text.strip() else ExtractionResult(source=src)


class OpenAIExtractor:
    """Optional LLM provider (vision). Off unless EXTRACTOR=openai and OPENAI_API_KEY set."""
    name = "openai"

    def __init__(self, api_key: str, model: str, fallback: Extractor):
        self.api_key, self.model, self.fallback = api_key, model, fallback

    def extract(self, path: Path, content_type: str) -> ExtractionResult:
        if content_type == "application/pdf":
            return self.fallback.extract(path, content_type)
        try:
            import httpx
            b64 = base64.b64encode(path.read_bytes()).decode()
            prompt = ("Extract from this receipt JSON with keys vendor (string), date (YYYY-MM-DD or null), "
                      "total (number, final amount paid, or null). Reply with JSON only.")
            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "response_format": {"type": "json_object"},
                      "messages": [{"role": "user", "content": [
                          {"type": "text", "text": prompt},
                          {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}}]}]},
                timeout=60)
            resp.raise_for_status()
            data = json.loads(resp.json()["choices"][0]["message"]["content"])
            d = None
            if data.get("date"):
                d = datetime.strptime(data["date"], "%Y-%m-%d").date()
            total = data.get("total")
            return ExtractionResult(vendor=str(data.get("vendor") or "")[:120], date=d,
                                    amount_cents=round(float(total) * 100) if total is not None else None,
                                    raw_text=json.dumps(data), source="openai")
        except Exception:
            log.exception("openai extraction failed; falling back to heuristic")
            return self.fallback.extract(path, content_type)


def build_extractor(settings) -> Extractor:
    heuristic = HeuristicExtractor()
    if settings.extractor == "none":
        return NullExtractor()
    if settings.extractor == "openai":
        if settings.openai_api_key:
            return OpenAIExtractor(settings.openai_api_key, settings.openai_model, heuristic)
        log.warning("EXTRACTOR=openai but OPENAI_API_KEY not set; using heuristic extractor")
    return heuristic
