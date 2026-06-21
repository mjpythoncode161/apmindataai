"""Match PDF buyer names to Trader records."""
from __future__ import annotations

import re


def _normalize_name(value: str) -> str:
    text = (value or "").upper()
    text = re.sub(r"[^A-Z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def match_trader_for_pdf_buyer(buyer_name: str, traders) -> tuple[object | None, str]:
    """
    Return (trader, match_note).
    traders: iterable of Trader model instances.
    """
    clean = _normalize_name(buyer_name)
    if not clean:
        return None, "empty"

    trader_list = list(traders)
    if not trader_list:
        return None, "no_traders"

    # Exact short_code match
    for t in trader_list:
        if t.short_code and _normalize_name(t.short_code) == clean:
            return t, "short_code_exact"

    # Exact name match
    for t in trader_list:
        if _normalize_name(t.name) == clean:
            return t, "name_exact"

    # Substring: PDF name contained in trader name or vice versa
    for t in trader_list:
        t_name = _normalize_name(t.name)
        if clean in t_name or t_name in clean:
            return t, "name_contains"

    # First significant token match (e.g. KEMBI, PATIL)
    tokens = [tok for tok in clean.split() if len(tok) >= 3]
    if tokens:
        best = None
        best_score = 0
        for t in trader_list:
            t_name = _normalize_name(t.name)
            t_short = _normalize_name(t.short_code or "")
            score = sum(1 for tok in tokens if tok in t_name or tok in t_short)
            if score > best_score:
                best_score = score
                best = t
        if best and best_score >= max(1, len(tokens) // 2):
            return best, "token_match"

    return None, "not_found"


def resolve_trader_for_pdf_row(row, traders):
    """
    Resolve trader from manual buyer_id / buyer_code, else PDF buyer name.
    Returns (trader_or_none, source).
    """
    trader_list = list(traders)
    if not trader_list:
        return None, "no_traders"

    buyer_id = row.get("buyer_id")
    if buyer_id not in (None, ""):
        try:
            bid = int(buyer_id)
            for t in trader_list:
                if t.id == bid:
                    return t, "manual_id"
        except (TypeError, ValueError):
            pass

    buyer_code = (row.get("buyer_code") or "").strip()
    if buyer_code:
        code_norm = _normalize_name(buyer_code)
        for t in trader_list:
            if t.short_code and _normalize_name(t.short_code) == code_norm:
                return t, "manual_code"
        for t in trader_list:
            if _normalize_name(t.name) == code_norm:
                return t, "manual_code"
        for t in trader_list:
            t_short = _normalize_name(t.short_code or "")
            t_name = _normalize_name(t.name)
            if code_norm in t_short or code_norm in t_name or t_short in code_norm:
                return t, "manual_code_partial"

    return match_trader_for_pdf_buyer(row.get("buyer_name", ""), trader_list)
