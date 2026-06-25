"""TDS on commission (Section 194H) for Kharidi Patti / TraderBill."""

from datetime import date
from decimal import Decimal, InvalidOperation

from django.db.models import Q, Sum

TDS_COMMISSION_THRESHOLD = Decimal("20000.00")
TDS_RATE = Decimal("0.02")
TDS_RATE_NO_PAN = Decimal("0.05")


def _to_decimal(value, default="0"):
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _quantize_money(value):
    try:
        return Decimal(value).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def financial_year_start(d):
    """Indian FY: 1 April – 31 March."""
    if d.month >= 4:
        return date(d.year, 4, 1)
    return date(d.year - 1, 4, 1)


def financial_year_end(d):
    start = financial_year_start(d)
    return date(start.year + 1, 3, 31)


def tds_rate_for_trader(trader):
    pan = (getattr(trader, "pan", None) or "").strip()
    return TDS_RATE if pan else TDS_RATE_NO_PAN


def _tds_from_cumulative(cumulative_before, commission, rate):
    """Return (tds_amount, tds_applicable) for one bill."""
    commission = _to_decimal(commission)
    cumulative_after = cumulative_before + commission
    if cumulative_after <= TDS_COMMISSION_THRESHOLD:
        return Decimal("0.00"), False

    if cumulative_before >= TDS_COMMISSION_THRESHOLD:
        tds_base = commission
    else:
        tds_base = cumulative_after - TDS_COMMISSION_THRESHOLD

    return _quantize_money(tds_base * rate), True


def calculate_bill_tds(bill):
    """TDS details for a single TraderBill (uses DB for prior FY commission)."""
    from accounts.models import TraderBill

    fy_start = financial_year_start(bill.date)
    prior = (
        TraderBill.objects.filter(
            buyer_id=bill.buyer_id,
            date__gte=fy_start,
        )
        .filter(
            Q(date__lt=bill.date)
            | Q(date=bill.date, id__lt=bill.id)
        )
        .aggregate(total=Sum("commission"))["total"]
    )
    cumulative_before = _to_decimal(prior)
    commission = _to_decimal(bill.commission)
    rate = tds_rate_for_trader(bill.buyer)
    tds, applicable = _tds_from_cumulative(cumulative_before, commission, rate)

    return {
        "commission": commission,
        "cumulative_commission": cumulative_before + commission,
        "tds": tds,
        "tds_applicable": applicable,
        "tds_rate_percent": _quantize_money(rate * Decimal("100")),
        "tds_status": "With TDS" if applicable else "Without TDS",
    }


def build_tds_report_rows(bills, filter_mode="all"):
    """
    Build report rows for TraderBill queryset/list.
    filter_mode: 'tds_only' | 'all'
    """
    from accounts.models import TraderBill

    bills = list(bills)
    if not bills:
        return []

    buyer_fy_keys = {(b.buyer_id, financial_year_start(b.date)) for b in bills}
    fy_bills_cache = {}

    for buyer_id, fy_start in buyer_fy_keys:
        fy_end = financial_year_end(fy_start)
        fy_bills_cache[(buyer_id, fy_start)] = list(
            TraderBill.objects.filter(
                buyer_id=buyer_id,
                date__range=[fy_start, fy_end],
            )
            .select_related("buyer")
            .order_by("date", "id")
        )

    rows = []
    for bill in sorted(bills, key=lambda b: (b.date, b.invoice_no or "", b.id)):
        key = (bill.buyer_id, financial_year_start(bill.date))
        cumulative = Decimal("0")
        rate = tds_rate_for_trader(bill.buyer)
        tds_info = None

        for fy_bill in fy_bills_cache.get(key, []):
            comm = _to_decimal(fy_bill.commission)
            if fy_bill.id == bill.id:
                tds, applicable = _tds_from_cumulative(cumulative, comm, rate)
                tds_info = {
                    "date": bill.date,
                    "bill_no": bill.invoice_no,
                    "bill_id": bill.id,
                    "buyer_name": bill.buyer.name,
                    "buyer_pan": (bill.buyer.pan or "").strip() or "-",
                    "buyer_gstin": (bill.buyer.gstin or "").strip() or "-",
                    "commission": comm,
                    "cumulative_commission": cumulative + comm,
                    "tds": tds,
                    "tds_applicable": applicable,
                    "tds_rate_percent": _quantize_money(rate * Decimal("100")),
                    "tds_status": "With TDS" if applicable else "Without TDS",
                    "total_amount": _to_decimal(bill.total_amount),
                    "grand_total": _to_decimal(bill.grand_total),
                }
                break
            cumulative += comm

        if tds_info is None:
            continue

        if filter_mode == "tds_only" and not tds_info["tds_applicable"]:
            continue
        rows.append(tds_info)

    return rows
