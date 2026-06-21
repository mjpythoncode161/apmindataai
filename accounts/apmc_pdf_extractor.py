"""Extract APMC BYD tender PDF rows (Trade Date, Lot Code, Rate, Buyer)."""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import BinaryIO

ROW_END_RE = re.compile(r"FARMER\s+(T\d+-\d+[A-Z])\s*$", re.IGNORECASE)
HEADER_TRADE_DATE_RE = re.compile(
    r"Trade Date\s*:\s*(\d{2}-[A-Za-z]{3}-\d{4})", re.IGNORECASE
)
ROW_PARSE_RE = re.compile(
    r"^(?P<sr>\d+)\s+"
    r"(?P<lot_code>L\d+)\s+"
    r"(?P<buyer>.+?)\s+"
    r"(?P<commodity>[A-Z][A-Z\s]+?)\s+"
    r"(?P<price>[\d,]+\.\d{2})\s+"
    r"(?P<trade_date>\d{2}-[A-Za-z]{3}-\d{4})\s+"
    r"(?:\d{2}:\d{2}\s+[AP]M\s+)?"
    r"(?P<bags>\d+)\s+"
    r"FARMER\s+"
    r"(?P<lot_id>T\d+-\d+[A-Z])\s*$",
    re.IGNORECASE,
)
SKIP_LINE_MARKERS = (
    "Commission Agent Name",
    "Lot Code Buyer",
    "APMC YARD",
    "CA COPY",
    "Farmer Name: Signature",
    "Sr No.",
    "Trade Price Trade Date",
    "/ Units",
)


def _extract_text_from_pdf(file_obj: BinaryIO) -> str:
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError(
            "PDF support is not installed. Run: pip install pdfplumber"
        ) from exc

    chunks = []
    with pdfplumber.open(file_obj) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            if page_text.strip():
                chunks.append(page_text)
    return "\n".join(chunks)


def _clean_buyer_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    name = re.sub(r"\s*\.\s*", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name


def normalize_lot_code(lot_code: str) -> str:
    """L0001 -> 1, L0010 -> 10."""
    code = (lot_code or "").strip().upper()
    if code.startswith("L"):
        numeric = code[1:].lstrip("0")
        return numeric or "0"
    return code.lstrip("0") or code


def parse_trade_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def _merge_pdf_lines(text: str) -> list[str]:
    records = []
    buffer: list[str] = []

    for raw_line in text.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        if any(marker in line for marker in SKIP_LINE_MARKERS):
            continue
        if re.fullmatch(r"\d+", line) and len(line) <= 2:
            continue

        buffer.append(line)
        joined = " ".join(buffer)
        if ROW_END_RE.search(joined):
            records.append(joined)
            buffer = []

    return records


def _parse_price(value: str) -> Decimal | None:
    if not value:
        return None
    try:
        cleaned = value.replace(",", "").strip()
        price = Decimal(cleaned)
        return price if price > 0 else None
    except (InvalidOperation, ValueError):
        return None


def extract_apmc_tender_pdf(file_obj: BinaryIO) -> dict:
    """
    Parse APMC BYD CA COPY tender PDF.
    Returns dict with trade_date, rows, and document-level errors.
    """
    text = _extract_text_from_pdf(file_obj)
    if not text.strip():
        return {
            "success": False,
            "errors": ["Could not read any text from the PDF file."],
            "trade_date": None,
            "rows": [],
        }

    header_match = HEADER_TRADE_DATE_RE.search(text)
    header_trade_date = (
        parse_trade_date(header_match.group(1)) if header_match else None
    )

    merged_rows = _merge_pdf_lines(text)
    if not merged_rows:
        return {
            "success": False,
            "errors": [
                "No lot records found in PDF. Please upload a valid APMC tender PDF."
            ],
            "trade_date": header_trade_date.isoformat() if header_trade_date else None,
            "rows": [],
        }

    rows = []
    row_errors = []

    for raw in merged_rows:
        match = ROW_PARSE_RE.match(raw)
        if not match:
            row_errors.append(f"Could not parse row: {raw[:80]}...")
            continue

        lot_code = match.group("lot_code").upper()
        lot_number = normalize_lot_code(lot_code)
        buyer_name = _clean_buyer_name(match.group("buyer"))
        price = _parse_price(match.group("price"))
        trade_date = parse_trade_date(match.group("trade_date"))
        bags = int(match.group("bags"))

        row = {
            "sr_no": int(match.group("sr")),
            "lot_code": lot_code,
            "lot_number": lot_number,
            "buyer_name": buyer_name,
            "trade_price": str(price) if price is not None else "",
            "trade_date": trade_date.isoformat() if trade_date else "",
            "no_of_bags": bags,
            "lot_id": match.group("lot_id"),
            "commodity": match.group("commodity").strip(),
            "errors": [],
        }

        if not trade_date:
            row["errors"].append("Trade Date is missing or invalid.")
        if price is None:
            row["errors"].append("Trade Price is missing or invalid.")
        if not lot_number:
            row["errors"].append("Lot Code is missing or invalid.")
        if not buyer_name:
            row["errors"].append("Buyer Name is missing.")

        rows.append(row)

    if not rows:
        return {
            "success": False,
            "errors": row_errors or ["No valid lot rows could be extracted from PDF."],
            "trade_date": header_trade_date.isoformat() if header_trade_date else None,
            "rows": [],
        }

    doc_trade_date = header_trade_date
    if not doc_trade_date:
        first_valid = next((r for r in rows if r.get("trade_date")), None)
        if first_valid:
            doc_trade_date = parse_trade_date(first_valid["trade_date"])

    return {
        "success": True,
        "errors": row_errors,
        "trade_date": doc_trade_date.isoformat() if doc_trade_date else None,
        "rows": rows,
    }
