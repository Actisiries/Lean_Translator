"""Step 1 – Text extraction & cleaning from PDF or raw string."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# Patterns that usually belong to headers / footers / noise
_NOISE_PATTERNS = [
    re.compile(r"^\s*\d+\s*$"),                          # lone page numbers
    re.compile(r"^\s*Page\s+\d+\s*(of\s+\d+)?\s*$", re.I),
    re.compile(r"^\s*arXiv:.*$", re.I),
    re.compile(r"^\s*©.*$"),
    re.compile(r"^\s*All rights reserved\.?\s*$", re.I),
    re.compile(r"^\s*References\s*$", re.I),
    re.compile(r"^\s*Bibliography\s*$", re.I),
]


def extract_from_pdf(path: str | Path, max_pages: Optional[int] = None) -> str:
    """
    Extract text from a PDF. Prefers pdfplumber; falls back to pypdf.
    Keeps mathematical notation as-is (best-effort).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    text = _try_pdfplumber(path, max_pages)
    if not text.strip():
        text = _try_pypdf(path, max_pages)
    return clean_math_text(text)


def _try_pdfplumber(path: Path, max_pages: Optional[int]) -> str:
    try:
        import pdfplumber
    except ImportError:
        return ""
    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        pages = pdf.pages[:max_pages] if max_pages else pdf.pages
        for page in pages:
            t = page.extract_text() or ""
            parts.append(t)
    return "\n\n".join(parts)


def _try_pypdf(path: Path, max_pages: Optional[int]) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    reader = PdfReader(str(path))
    pages = reader.pages[:max_pages] if max_pages else reader.pages
    parts = []
    for page in pages:
        parts.append(page.extract_text() or "")
    return "\n\n".join(parts)


def clean_math_text(raw: str) -> str:
    """
    Remove headers, footers, page numbers, reference sections and
    other irrelevant material while preserving mathematical content.
    """
    if not raw:
        return ""

    lines = raw.splitlines()
    cleaned: list[str] = []
    in_references = False

    for line in lines:
        stripped = line.strip()

        # Drop everything after a References / Bibliography heading
        if re.match(r"^(References|Bibliography)\s*$", stripped, re.I):
            in_references = True
            continue
        if in_references:
            continue

        # Drop pure noise lines
        if any(p.match(stripped) for p in _NOISE_PATTERNS):
            continue

        # Collapse excessive whitespace but keep blank lines as paragraph breaks
        if not stripped:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue

        cleaned.append(stripped)

    text = "\n".join(cleaned)

    # Normalize common LaTeX-ish artifacts that OCR sometimes produces
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def load_text_or_pdf(
    text: Optional[str] = None,
    pdf_path: Optional[str | Path] = None,
    max_pages: Optional[int] = None,
) -> str:
    """Convenience entry: either a raw string or a PDF path.

    Returns empty string when neither usable text nor PDF is provided so
    callers can report a clean failure instead of raising.
    """
    if text is not None and str(text).strip():
        return clean_math_text(text)
    if pdf_path is not None and str(pdf_path).strip():
        return extract_from_pdf(pdf_path, max_pages=max_pages)
    return ""
