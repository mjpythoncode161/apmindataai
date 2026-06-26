from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from decimal import Decimal, InvalidOperation
from django.db import IntegrityError, transaction, connection
from django.http import JsonResponse, HttpResponseForbidden
from django.db.models import Q, Sum, Count


from .models import (
    Users,
    Farmer,
    Trader,
    Avak,
    Bikri,
    BikriBagWeight,
    BagTransfer,
    BagTransferWeight,
    MarketRate,
    BankMaster,
    CompanyProfile,
    TraderBill,
    TraderBillItem,
    FinancialTransaction,
    LedgerAccount,
    LedgerGroup,
    Voucher,
    VoucherLine,
)
from .ledger_defaults import ensure_default_ledgers

from datetime import date, timedelta
import calendar
import json

# Default per-bag weight (kg) for avak lots before Bikri weighing (Lot Detail Modification).
DEFAULT_AVG_BAG_WEIGHT_KG = Decimal("30")


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_decimal(value, default="0"):
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _get_rakham_percent_for_date(target_date):
    """Return rakham_percent from MarketRate for the given date (or closest preceding).
    Falls back to 0 when no rates exist."""
    if not target_date:
        return Decimal("0")

    rates = (
        MarketRate.objects.filter(date__lte=target_date)
        .order_by("-date")
        .only("rakham_percent")
        .first()
    )
    if not rates or rates.rakham_percent is None:
        return Decimal("0")

    percent = rates.rakham_percent
    if percent < 0:
        return Decimal("0")
    if percent > 100:
        return Decimal("100")
    return percent


def _quantize_money(value):
    try:
        return Decimal(value).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def _format_indian_amount(amount):
    """Format amount Indian style e.g. 11,46,601.00; zero -> .00"""
    if amount is None:
        return ".00"
    amt = _quantize_money(abs(amount))
    if amt == 0:
        return ".00"
    int_part, _, dec_part = f"{amt:.2f}".partition(".")
    if len(int_part) <= 3:
        grouped = int_part
    else:
        last3 = int_part[-3:]
        rest = int_part[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts + [last3])
    prefix = "-" if amount < 0 else ""
    return f"{prefix}{grouped}.{dec_part}"


def _cash_bank_ledgers_qs():
    """Cash + all bank ledgers (Bank Accounts and legacy Cash & Bank group)."""
    from django.db.models import Q

    return LedgerAccount.objects.filter(
        Q(group__name="Bank Accounts") | Q(group__name="Cash & Bank")
    ).select_related("group", "farmer", "trader").order_by("name")


def _is_cash_in_hand_ledger(ledger):
    return ledger.name.strip().lower() == CASH_IN_HAND_LEDGER_NAME.lower()


def _ledger_place_for_trial(ledger):
    """Place column for trial balance rows."""
    if ledger.farmer_id:
        place = (
            Avak.objects.filter(farmer_id=ledger.farmer_id)
            .exclude(place="")
            .order_by("-id")
            .values_list("place", flat=True)
            .first()
        )
        if place:
            return place
        if ledger.farmer.address:
            return ledger.farmer.address[:40]
        return "*-*"
    if ledger.trader_id:
        if ledger.trader.address:
            return ledger.trader.address[:40]
        return "Byadgi"
    return "*-*"


def _party_voucher_dr_cr(line, entity_type):
    """
    Dr/Cr columns for farmer/trader account statements from voucher lines.
    Farmer (creditor): payment to farmer = Dr; receipt from farmer = Cr.
    Trader (debtor): receipt from trader = Cr; payment to trader = Dr.
    """
    amt = line.amount
    vtype = line.voucher.voucher_type
    if entity_type == "farmer":
        if vtype == "Payment":
            return amt, Decimal("0")
        if vtype == "Receipt":
            return Decimal("0"), amt
    elif entity_type == "trader":
        if vtype == "Receipt":
            return Decimal("0"), amt
        if vtype == "Payment":
            return amt, Decimal("0")
    if line.entry_type == "Dr":
        return amt, Decimal("0")
    return Decimal("0"), amt


def _calc_ledger_trial_gross_dr_cr(ledger, as_on_date, skip_ft_pay=False):
    """
    Trial balance: gross debit and credit movements.
    skip_ft_pay: bank ledgers — omit payment FT + Payment vouchers (in Sundry Creditors).
    """
    total_dr = Decimal("0")
    total_cr = Decimal("0")

    if ledger.balance_type == "Dr":
        total_dr += ledger.opening_balance
    else:
        total_cr += ledger.opening_balance

    vl_qs = VoucherLine.objects.filter(
        ledger=ledger, voucher__date__lte=as_on_date
    ).select_related("voucher")
    for line in vl_qs:
        if skip_ft_pay and line.voucher.voucher_type == "Payment":
            continue
        if ledger.farmer_id:
            dr_amt, cr_amt = _party_voucher_dr_cr(line, "farmer")
        elif ledger.trader_id:
            dr_amt, cr_amt = _party_voucher_dr_cr(line, "trader")
        else:
            dr_amt = line.amount if line.entry_type == "Dr" else Decimal("0")
            cr_amt = line.amount if line.entry_type == "Cr" else Decimal("0")
        total_dr += dr_amt
        total_cr += cr_amt

    if not skip_ft_pay:
        for ft in FinancialTransaction.objects.filter(
            pay_from_ledger=ledger, date__lte=as_on_date
        ).only("transaction_type", "amount"):
            if ft.transaction_type == "Debit":
                total_cr += ft.amount
            else:
                total_dr += ft.amount

    if not skip_ft_pay:
        for ft in FinancialTransaction.objects.filter(
            debit_ledger=ledger, date__lte=as_on_date
        ).only("amount"):
            total_dr += ft.amount

    if ledger.farmer_id:
        for ft in FinancialTransaction.objects.filter(
            farmer_id=ledger.farmer_id, date__lte=as_on_date
        ).only("transaction_type", "amount"):
            if ft.transaction_type == "Debit":
                total_dr += ft.amount
            else:
                total_cr += ft.amount
    elif ledger.trader_id:
        for ft in FinancialTransaction.objects.filter(
            trader_id=ledger.trader_id, date__lte=as_on_date
        ).only("transaction_type", "amount"):
            if ft.transaction_type == "Credit":
                total_cr += ft.amount
            else:
                total_dr += ft.amount

    return _quantize_money(total_dr), _quantize_money(total_cr)


def _calc_ledger_trial_display_dr_cr(ledger, as_on_date, skip_ft_pay=False):
    """
    Trial balance row: net closing balance in one column only (Dr or Cr).
  skip_ft_pay: bank ledgers — hide payment FT totals already in Sundry Creditors.
    """
    gross_dr, gross_cr = _calc_ledger_trial_gross_dr_cr(
        ledger, as_on_date, skip_ft_pay=skip_ft_pay
    )
    net = gross_cr - gross_dr
    if net >= 0:
        return Decimal("0"), _quantize_money(net)
    return _quantize_money(abs(net)), Decimal("0")


def _calc_ledger_closing_as_on(ledger, as_on_date):
    """Net ledger balance as on date → (debit_display, credit_display) for trial balance."""
    running = (
        ledger.opening_balance
        if ledger.balance_type == "Dr"
        else -ledger.opening_balance
    )

    vl_qs = VoucherLine.objects.filter(
        ledger=ledger, voucher__date__lte=as_on_date
    ).select_related("voucher").only("entry_type", "amount", "voucher__voucher_type")
    for line in vl_qs:
        if ledger.farmer_id:
            dr_amt, cr_amt = _party_voucher_dr_cr(line, "farmer")
            running += dr_amt - cr_amt
        elif ledger.trader_id:
            dr_amt, cr_amt = _party_voucher_dr_cr(line, "trader")
            running += dr_amt - cr_amt
        elif line.entry_type == "Dr":
            running += line.amount
        else:
            running -= line.amount

    ft_pay = FinancialTransaction.objects.filter(
        pay_from_ledger=ledger, date__lte=as_on_date
    ).only("transaction_type", "amount")
    for ft in ft_pay:
        if ft.transaction_type == "Debit":
            running -= ft.amount
        else:
            running += ft.amount

    ft_dr = FinancialTransaction.objects.filter(
        debit_ledger=ledger, date__lte=as_on_date
    ).only("amount")
    for ft in ft_dr:
        running += ft.amount

    if ledger.farmer_id:
        # Farmer is a creditor: bikri → Cr; payment to farmer → Dr (reduces Cr).
        ft_farmer = FinancialTransaction.objects.filter(
            farmer_id=ledger.farmer_id, date__lte=as_on_date
        ).only("transaction_type", "amount")
        for ft in ft_farmer:
            if ft.transaction_type == "Debit":
                running += ft.amount
            else:
                running -= ft.amount
    elif ledger.trader_id:
        ft_trader = FinancialTransaction.objects.filter(
            trader_id=ledger.trader_id, date__lte=as_on_date
        ).only("transaction_type", "amount")
        for ft in ft_trader:
            if ft.transaction_type == "Credit":
                running -= ft.amount
            else:
                running += ft.amount

    running = _quantize_money(running)
    if running >= 0:
        return running, Decimal("0")
    return Decimal("0"), abs(running)


def _signed_ledger_balance(ledger, as_on_date):
    """Net balance: positive = Dr, negative = Cr."""
    dr, cr = _calc_ledger_closing_as_on(ledger, as_on_date)
    return dr - cr


def _ledger_period_change(ledger, from_date, to_date):
    """Signed balance change between dates (inclusive period)."""
    if from_date:
        start = _signed_ledger_balance(ledger, from_date - timedelta(days=1))
    else:
        start = (
            ledger.opening_balance
            if ledger.balance_type == "Dr"
            else -ledger.opening_balance
        )
    end = _signed_ledger_balance(ledger, to_date)
    return end - start


def _ledger_pl_amount(ledger, from_date, to_date):
    """Period P&L amount for an income or expense ledger."""
    change = _ledger_period_change(ledger, from_date, to_date)
    if ledger.group.nature == "Income":
        return max(Decimal("0"), -change)
    if ledger.group.nature == "Expenses":
        return max(Decimal("0"), change)
    return Decimal("0")


def _fmt_pl(amount):
    return _format_indian_amount(amount) if amount else ".00"


def _group_sort_key(group):
    nature_order = {"Assets": 0, "Liabilities": 1, "Income": 2, "Expenses": 3}
    return (nature_order.get(group.nature, 9), group.name.lower())


# Trial Balance section heads (Tally-style, per company format)
TRIAL_BALANCE_HEAD_ORDER = [
    "Bank Accounts",
    "Bank OD A/C",
    "Capital Account",
    "Current Assets",
    "Direct Incomes",
    "Trading Account",
    "Indirect Expenses",
    "Sundry Creditors",
    "Sundry Debtors",
    "Unsecured Loan",
    "Others",
    "Provision(Payable)",
]

# Omitted from Trial Balance and Profit & Loss income (trader charges → Commission only)
PL_EXCLUDE_INCOME_LEDGER_NAMES = frozenset({
    "Farmer Deductions Income",
    "Hamali",
    "Packing Income",
    "Weighman Fee Income",
})
TRIAL_BALANCE_EXCLUDE_LEDGER_NAMES = PL_EXCLUDE_INCOME_LEDGER_NAMES

# Only this ledger appears under Trial Balance → Direct Incomes (trader ಖರೀದಿ ಪಟ್ಟಿ)
TRIAL_BALANCE_DIRECT_INCOME_LEDGER = "Commission Account"

# Trial Balance heads that list every ledger even when closing balance is zero
TRIAL_BALANCE_SHOW_ALL_LEDGER_HEADS = frozenset({
    "Bank Accounts",
    "Bank OD A/C",
})

CASH_IN_HAND_LEDGER_NAME = "Cash in Hand"

# Legacy / internal ledger group → trial balance head
_GROUP_TO_TRIAL_HEAD = {
    "Bank Accounts": "Bank Accounts",
    "Bank OD A/C": "Bank OD A/C",
    "Capital Account": "Capital Account",
    "Current Assets": "Current Assets",
    "Direct Incomes": "Direct Incomes",
    "Trading Account": "Trading Account",
    "Indirect Expenses": "Indirect Expenses",
    "Sundry Creditors": "Sundry Creditors",
    "Sundry Debtors": "Sundry Debtors",
    "Unsecured Loan": "Unsecured Loan",
    "Others": "Others",
    "Provision(Payable)": "Provision(Payable)",
    "Fixed Assets": "Current Assets",
    "Other Assets": "Current Assets",
    "Loans & Borrowings": "Unsecured Loan",
    "Other Liabilities": "Provision(Payable)",
    "Other Income": "Others",
    "Commission Income": "Direct Incomes",
    "Rent & Rates": "Indirect Expenses",
    "Electricity & Water": "Indirect Expenses",
    "Salary & Wages": "Indirect Expenses",
    "Office Expenses": "Indirect Expenses",
    "Communication": "Indirect Expenses",
    "Printing & Stationery": "Indirect Expenses",
    "Repairs & Maintenance": "Indirect Expenses",
    "Transportation": "Indirect Expenses",
    "Miscellaneous": "Indirect Expenses",
    "Expenses": "Indirect Expenses",
}


def _is_trading_income_ledger(ledger):
    """Commodity / trading ledgers → Trading Account head."""
    if ledger.group.name == "Trading Account":
        return True
    name_l = ledger.name.lower()
    trading_keywords = (
        "chillies", "chilly", "chilli", "dry chilli", "commodity",
        "trading account", "purchase account", "sales account",
    )
    return any(kw in name_l for kw in trading_keywords)


def _trial_balance_head_for_ledger(ledger):
    """Map a ledger to its Trial Balance section head, or None to omit."""
    if ledger.name in TRIAL_BALANCE_EXCLUDE_LEDGER_NAMES:
        return None
    if _is_cash_in_hand_ledger(ledger):
        return None

    if ledger.name == "Cess Income":
        return "Provision(Payable)"

    group_name = ledger.group.name
    name_l = ledger.name.lower()

    if group_name == "Direct Incomes":
        if ledger.name == TRIAL_BALANCE_DIRECT_INCOME_LEDGER:
            return "Direct Incomes"
        return None

    if group_name in ("Cash & Bank", "Bank Accounts"):
        if " od" in name_l or name_l.endswith(" od ac") or "overdraft" in name_l:
            return "Bank OD A/C"
        return "Bank Accounts"

    if group_name == "Commission Income":
        if _is_trading_income_ledger(ledger):
            return "Trading Account"
        return "Direct Incomes"

    head = _GROUP_TO_TRIAL_HEAD.get(group_name)
    if head:
        return head

    if ledger.group.nature == "Expenses":
        return "Indirect Expenses"
    if ledger.group.nature == "Income":
        return "Direct Incomes" if not _is_trading_income_ledger(ledger) else "Trading Account"

    return "Others"


def _trial_balance_head_sort_key(head):
    try:
        return TRIAL_BALANCE_HEAD_ORDER.index(head)
    except ValueError:
        return len(TRIAL_BALANCE_HEAD_ORDER)


def _calculate_rakham_amount(amount, rakham_percent):
    amount = _to_decimal(amount)
    rakham_percent = _to_decimal(rakham_percent)
    if rakham_percent < 0:
        rakham_percent = Decimal("0")
    if rakham_percent > 100:
        rakham_percent = Decimal("100")
    return _quantize_money((amount * rakham_percent) / Decimal("100"))


def _calculate_net_payable_for_bikri(bikri, rakham_percent):
    """Calculate net payable using the current Rakham % setting.

    Formula:
      bill_amount = amount - farmer_hamali + farmer_packing
      net_payable = bill_amount - rakham_amount - rent - unload_fee - other_fee_1 - other_fee_2
    """
    rakham_amount = _calculate_rakham_amount(bikri.amount, rakham_percent)
    bill_amount = bikri.amount - bikri.farmer_hamali + bikri.farmer_packing
    net_payable = (
        bill_amount
        - rakham_amount
        - bikri.rent
        - bikri.unload_fee
        - bikri.other_fee_1
        - bikri.other_fee_2
    )
    return _quantize_money(net_payable), rakham_amount


def _used_lot_numbers_for_date(entry_date, exclude_bikri_id=None):
    """Numeric lot numbers fully sold (all bags) on this date."""
    used = []
    seen_avak = {}
    qs = Bikri.objects.filter(date=entry_date, is_cancelled=False).select_related("avak")
    if exclude_bikri_id:
        qs = qs.exclude(id=exclude_bikri_id)
    for bikri in qs:
        if not bikri.avak:
            continue
        avak_id = bikri.avak_id
        seen_avak.setdefault(avak_id, {"avak": bikri.avak, "bags": 0})
        seen_avak[avak_id]["bags"] += int(bikri.no_of_bags or 0)

    for info in seen_avak.values():
        avak = info["avak"]
        avak_bags = int(avak.no_of_bags or 0)
        if avak_bags > 0 and info["bags"] >= avak_bags:
            lot_no = str(avak.lot_number or "").strip()
            if lot_no.isdigit():
                used.append(int(lot_no))
    return sorted(set(used))


def _get_avak_bikri_coverage(avak, entry_date, exclude_bikri_id=None):
    """Bag coverage for split sales: multiple buyers on the same lot/date."""
    qs = Bikri.objects.filter(
        avak=avak, date=entry_date, is_cancelled=False
    ).select_related("buyer").prefetch_related("weights")
    if exclude_bikri_id:
        qs = qs.exclude(id=exclude_bikri_id)

    avak_bags = int(avak.no_of_bags or 0)
    used_bag_nos = set()
    total_sold = 0
    segments = []
    for bikri in qs:
        weight_rows = list(bikri.weights.order_by("bag_no").values("bag_no", "weight"))
        bag_nos = [w["bag_no"] for w in weight_rows]
        used_bag_nos.update(bag_nos)
        total_sold += int(bikri.no_of_bags or 0)
        buyer = bikri.buyer
        segments.append(
            {
                "bikri_id": bikri.id,
                "buyer_id": bikri.buyer_id,
                "buyer_name": (buyer.short_code or buyer.name) if buyer else "",
                "bags": bikri.no_of_bags,
                "weight": float(bikri.total_weight or 0),
                "rate": float(bikri.rate or 0),
                "amount": float(bikri.amount or 0),
                "farmer_hamali": float(bikri.farmer_hamali or 0),
                "farmer_packing": float(bikri.farmer_packing or 0),
                "bag_nos": bag_nos,
                "weights": [
                    {"bag_no": w["bag_no"], "weight": float(w["weight"])}
                    for w in weight_rows
                ],
            }
        )

    remaining = max(0, avak_bags - total_sold)
    fully_sold = avak_bags > 0 and total_sold >= avak_bags
    if fully_sold and used_bag_nos:
        fully_sold = set(range(1, avak_bags + 1)).issubset(used_bag_nos)

    return {
        "avak_bags": avak_bags,
        "total_sold": total_sold,
        "remaining": remaining,
        "used_bag_nos": used_bag_nos,
        "fully_sold": fully_sold,
        "segments": segments,
    }


def _validate_bikri_bag_entry(avak, entry_date, no_of_bags, weights_data, exclude_bikri_id=None):
    """Validate partial or full vikri bag counts for split-buyer sales."""
    coverage = _get_avak_bikri_coverage(avak, entry_date, exclude_bikri_id)
    avak_bags = coverage["avak_bags"]

    if no_of_bags <= 0:
        raise ValueError("Number of bags must be at least 1.")
    if no_of_bags > coverage["remaining"]:
        raise ValueError(
            f"Lot {avak.lot_number} has {coverage['remaining']} bag(s) remaining "
            f"({coverage['total_sold']} of {avak_bags} already sold)."
        )

    new_bag_nos = set()
    if weights_data:
        weights = json.loads(weights_data)
        new_bag_nos = {int(w["bag_no"]) for w in weights}
        if len(new_bag_nos) != no_of_bags:
            raise ValueError(
                f"Bag count ({no_of_bags}) must match individual weights entered ({len(new_bag_nos)})."
            )
        overlap = new_bag_nos & coverage["used_bag_nos"]
        if overlap:
            nums = ", ".join(str(n) for n in sorted(overlap))
            raise ValueError(f"Bag(s) {nums} already sold on lot {avak.lot_number}.")

    if coverage["fully_sold"]:
        raise ValueError(
            f"Lot {avak.lot_number} already has a complete Vikri entry for this date."
        )

    return coverage


def _bikri_edit_segment_payload(bikri):
    """Full segment payload for multi-buyer edit on the same lot."""
    weight_rows = list(bikri.weights.order_by("bag_no").values("bag_no", "weight"))
    buyer = bikri.buyer
    weights = [
        {"bag_no": w["bag_no"], "weight": float(w["weight"])} for w in weight_rows
    ]
    return {
        "bikri_id": bikri.id,
        "buyer_id": bikri.buyer_id,
        "buyer_name": (buyer.short_code or buyer.name) if buyer else "",
        "buyer_full_name": buyer.name if buyer else "",
        "bags": bikri.no_of_bags,
        "weight": float(bikri.total_weight or 0),
        "rate": float(bikri.rate or 0),
        "amount": float(bikri.amount or 0),
        "farmer_hamali": float(bikri.farmer_hamali or 0),
        "farmer_packing": float(bikri.farmer_packing or 0),
        "net_payable": float(bikri.net_payable or 0),
        "weights": weights,
        "weights_json": json.dumps(weights),
        "total_weight": str(bikri.total_weight or 0),
        "dalali": str(bikri.dalali or 0),
        "cess": str(bikri.cess or 0),
        "gst": str(bikri.gst or 0),
        "hamali": str(bikri.hamali or 0),
        "packing": str(bikri.packing or 0),
        "weighman_fee": str(bikri.weighman_fee or 0),
        "total_amount": str(bikri.total_amount or 0),
        "rent": str(bikri.rent or 0),
        "unload_fee": str(bikri.unload_fee or 0),
        "cash_deduct": str(bikri.cash_deduct or 0),
        "other_fee_1": str(bikri.other_fee_1 or 0),
        "other_fee_2": str(bikri.other_fee_2 or 0),
        "hamali_rate": str(bikri.hamali_rate or 0),
        "packing_rate": str(bikri.packing_rate or 0),
        "dalali_rate": str(bikri.dalali_rate or 0),
        "cess_rate": str(bikri.cess_rate or 0),
        "gst_rate": str(bikri.gst_rate or 0),
        "farmer_hamali_rate": str(bikri.farmer_hamali_rate or 0),
        "farmer_packing_rate": str(bikri.farmer_packing_rate or 0),
        "farmer_unloading_rate": str(bikri.farmer_unloading_rate or 0),
    }


def _lot_number_sort_key(lot_no):
    lot_no = str(lot_no or "").strip()
    if lot_no.isdigit():
        return (0, int(lot_no), lot_no)
    return (1, 0, lot_no)


def _group_bikris_by_lot_for_bill(bikri_list):
    """One display row per lot — merges split-buyer segments on the farmer bill."""
    from collections import OrderedDict

    grouped = OrderedDict()
    for b in bikri_list:
        lot_no = str(b.avak.lot_number if b.avak else "").strip()
        key = lot_no or f"avak_{b.avak_id}"
        if key not in grouped:
            grouped[key] = {
                "lot_number": lot_no,
                "no_of_bags": 0,
                "total_weight": Decimal("0"),
                "amount": Decimal("0"),
            }
        g = grouped[key]
        g["no_of_bags"] += int(b.no_of_bags or 0)
        g["total_weight"] += _to_decimal(b.total_weight)
        g["amount"] += _to_decimal(b.amount)

    result = []
    for g in grouped.values():
        weight = g["total_weight"]
        amount = g["amount"]
        if weight > 0:
            rate = (amount * Decimal("100") / weight).quantize(Decimal("0.01"))
        else:
            rate = Decimal("0")
        result.append(
            {
                "lot_number": g["lot_number"],
                "no_of_bags": g["no_of_bags"],
                "total_weight": weight,
                "rate": rate,
                "amount": amount,
            }
        )

    result.sort(key=lambda row: _lot_number_sort_key(row["lot_number"]))
    return result


def _sum_avak_deductions_once(bikri_list):
    """Sum avak-level deductions once per lot (avoids double count on split sales)."""
    seen = set()
    rent = unload_fee = other_fee_1 = other_fee_2 = Decimal("0")
    for b in bikri_list:
        if not b.avak_id or b.avak_id in seen:
            continue
        seen.add(b.avak_id)
        rent += _to_decimal(b.avak.freight)
        unload_fee += _to_decimal(b.avak.hamali_total)
        other_fee_1 += _to_decimal(b.avak.empty_bags)
        other_fee_2 += _to_decimal(b.avak.advance)
    return rent, unload_fee, other_fee_1, other_fee_2


def _avak_deduction_total(avak):
    if not avak:
        return Decimal("0")
    return (
        _to_decimal(avak.freight)
        + _to_decimal(avak.hamali_total)
        + _to_decimal(avak.empty_bags)
        + _to_decimal(avak.advance)
    )


def _group_net_payable_rates(group, sale_date):
    """Market/farmer rates shared by view_bikri and chopada."""
    market_rates = MarketRate.objects.filter(date=sale_date).first()
    if market_rates:
        return (
            _to_decimal(market_rates.farmer_hamali_per_bag),
            _to_decimal(market_rates.farmer_packing_per_bag),
            _to_decimal(market_rates.rakham_percent),
        )
    first = group[0]
    return (
        _to_decimal(first.farmer_hamali_rate or first.hamali_rate),
        _to_decimal(first.farmer_packing_rate or first.packing_rate),
        _get_rakham_percent_for_date(sale_date),
    )


def _calc_group_net_payable_breakdown(group, sale_date):
    """Per-bikri net payable rows that sum to the combined view_bikri total."""
    if not group:
        return Decimal("0"), []

    group = sorted(
        group,
        key=lambda b: (
            _lot_number_sort_key(b.avak.lot_number if b.avak else ""),
            b.id,
        ),
    )

    total_bags = sum(int(b.no_of_bags or 0) for b in group)
    total_amount = sum(_to_decimal(b.amount) for b in group)
    farmer_hamali_rate, farmer_packing_rate, rakham_percent = _group_net_payable_rates(
        group, sale_date
    )

    farmer_hamali = Decimal(str(total_bags)) * farmer_hamali_rate
    farmer_packing = Decimal(str(total_bags)) * farmer_packing_rate
    rent, unload_fee, other_fee_1, other_fee_2 = _sum_avak_deductions_once(group)
    total_deductions = rent + unload_fee + other_fee_1 + other_fee_2

    rakham_total = _calculate_rakham_amount(total_amount, rakham_percent)
    bill_amount = total_amount - farmer_hamali + farmer_packing
    total_net = _quantize_money(bill_amount - rakham_total - total_deductions)

    avak_deduction = {}
    for b in group:
        if b.avak_id and b.avak_id not in avak_deduction:
            avak_deduction[b.avak_id] = _avak_deduction_total(b.avak)

    avak_deduction_assigned = set()
    rows = []
    allocated_net = Decimal("0")

    for idx, b in enumerate(group):
        amount = _to_decimal(b.amount)
        bags = int(b.no_of_bags or 0)
        lot_hamali = Decimal(str(bags)) * farmer_hamali_rate
        lot_packing = Decimal(str(bags)) * farmer_packing_rate
        lot_bill = amount - lot_hamali + lot_packing

        if total_amount > 0:
            rakham_share = _quantize_money(rakham_total * amount / total_amount)
        else:
            rakham_share = Decimal("0")

        lot_avak_deduction = Decimal("0")
        if b.avak_id and b.avak_id not in avak_deduction_assigned:
            avak_deduction_assigned.add(b.avak_id)
            lot_avak_deduction = avak_deduction.get(b.avak_id, Decimal("0"))

        if idx == len(group) - 1:
            lot_net = _quantize_money(total_net - allocated_net)
        else:
            lot_net = _quantize_money(lot_bill - rakham_share - lot_avak_deduction)
            allocated_net += lot_net

        rows.append(
            {
                "bikri": b,
                "net_payable": lot_net,
                "deduction": rakham_share + lot_avak_deduction,
                "rakham_share": rakham_share,
            }
        )

    return total_net, rows


def _calc_group_net_payable(group, sale_date):
    """Calculate combined net payable for a group of bikris on the same date.

    Uses the same logic as view_bikri: dynamic market rates + avak fields,
    so that farmer_ledger and bikri view always show the same net amount.
    """
    total_net, _ = _calc_group_net_payable_breakdown(group, sale_date)
    return total_net


def _farmer_display_name(farmer, override=None):
    if override:
        return override
    if not farmer:
        return "-"
    return farmer.name_kannada or farmer.name or "-"


def _farmer_display_place(farmer, avak_place=None, override=None):
    if override:
        return override
    if farmer and farmer.address_kannada:
        return farmer.address_kannada
    if avak_place:
        return avak_place
    if farmer and farmer.address:
        return farmer.address
    return "-"


@login_required
def farmer_list(request):
    selected_date = _parse_account_date(request)
    date_str = selected_date.isoformat()

    farmers = Farmer.objects.filter(created_at__date=selected_date).order_by("-created_at")
    q = request.GET.get("q", "").strip()
    if q:
        farmers = farmers.filter(
            Q(name__icontains=q) | Q(name_kannada__icontains=q) | Q(phone__icontains=q)
        )

    form_values = {}

    if request.method == "POST":
        name = request.POST.get("full_name", "").strip()
        name_kannada = request.POST.get("name_kannada", "").strip()
        phone = request.POST.get("contact", "").strip()
        address = request.POST.get("address", "").strip()
        address_kannada = request.POST.get("address_kannada", "").strip()
        opening_balance = request.POST.get("opening_balance") or 0
        balance_type = request.POST.get("balance_type")
        ifsc = request.POST.get("ifsc", "").strip()
        account_no = request.POST.get("account_no", "").strip()
        bank_name = request.POST.get("bank_name", "").strip()
        branch_name = request.POST.get("branch_name", "").strip()

        form_values = {
            "name": name,
            "name_kannada": name_kannada,
            "phone": phone,
            "address": address,
            "address_kannada": address_kannada,
            "opening_balance": opening_balance,
            "balance_type": balance_type,
            "ifsc": ifsc,
            "account_no": account_no,
            "bank_name": bank_name,
            "branch_name": branch_name,
        }

        if not name:
            messages.error(request, "Name is required.")
        elif Farmer.objects.filter(name__iexact=name).exists():
            messages.error(request, f"Farmer '{name}' already exists. Use a unique name.")
        else:
            Farmer.objects.create(
                name=name,
                name_kannada=name_kannada,
                phone=phone,
                address=address,
                address_kannada=address_kannada,
                opening_balance=opening_balance,
                balance_type=balance_type,
                ifsc=ifsc,
                account_no=account_no,
                bank_name=bank_name,
                branch_name=branch_name,
            )
            messages.success(request, "Farmer added successfully.")
            return redirect(f"{reverse('farmer_list')}?date={date_str}")

    show_form = request.GET.get("add") == "1" or request.method == "POST" or bool(form_values)

    return render(request, "accounts/farmer_list.html", {
        "date_value": date_str,
        "farmers": farmers,
        "form_values": form_values,
        "q": q,
        "show_form": show_form,
    })


def _parse_account_date(request):
    date_param = request.GET.get("date") or request.POST.get("list_date")
    if date_param:
        try:
            return date.fromisoformat(date_param)
        except ValueError:
            pass
    return date.today()


@login_required
def account_hub(request):
    return redirect("farmer_list")


@login_required
def add_farmer(request):
    if request.method == "POST":
        name = request.POST.get("full_name", "").strip()
        name_kannada = request.POST.get("name_kannada", "").strip()
        phone = request.POST.get("contact", "").strip()
        address = request.POST.get("address", "").strip()
        address_kannada = request.POST.get("address_kannada", "").strip()

        # Bank Details
        opening_balance = request.POST.get("opening_balance") or 0
        balance_type = request.POST.get("balance_type")
        ifsc = request.POST.get("ifsc", "").strip()
        account_no = request.POST.get("account_no", "").strip()
        bank_name = request.POST.get("bank_name", "").strip()
        branch_name = request.POST.get("branch_name", "").strip()

        # Duplicate check
        if Farmer.objects.filter(name__iexact=name).exists():
            messages.error(request, f"Farmer with name '{name}' already exists. Please use a unique name (e.g., add an initial).")
            form_values = {
                "name": name,
                "name_kannada": name_kannada,
                "phone": phone,
                "address": address,
                "address_kannada": address_kannada,
                "opening_balance": opening_balance,
                "balance_type": balance_type,
                "ifsc": ifsc,
                "account_no": account_no,
                "bank_name": bank_name,
                "branch_name": branch_name,
            }
            return render(request, "accounts/add_farmer.html", {"form_values": form_values, "farmer": None})

        Farmer.objects.create(
            name=name,
            name_kannada=name_kannada,
            phone=phone,
            address=address,
            address_kannada=address_kannada,
            opening_balance=opening_balance,
            balance_type=balance_type,
            ifsc=ifsc,
            account_no=account_no,
            bank_name=bank_name,
            branch_name=branch_name,
        )
        messages.success(request, "Farmer added successfully.")
        return redirect("farmer_list")

    return redirect(f"{reverse('farmer_list')}?add=1")


@login_required
def edit_farmer(request, farmer_id):
    farmer = Farmer.objects.get(id=farmer_id)
    if request.method == "POST":
        name = request.POST.get("full_name", "").strip()
        name_kannada = request.POST.get("name_kannada", "").strip()
        phone = request.POST.get("contact", "").strip()
        address = request.POST.get("address", "").strip()
        address_kannada = request.POST.get("address_kannada", "").strip()

        opening_balance = request.POST.get("opening_balance") or 0
        balance_type = request.POST.get("balance_type")
        ifsc = request.POST.get("ifsc", "").strip()
        account_no = request.POST.get("account_no", "").strip()
        bank_name = request.POST.get("bank_name", "").strip()
        branch_name = request.POST.get("branch_name", "").strip()

        # Duplicate check (excluding current farmer)
        if Farmer.objects.filter(name__iexact=name).exclude(id=farmer_id).exists():
            messages.error(request, f"Another farmer with name '{name}' already exists. Please use a unique name.")
            form_values = {
                "name": name,
                "name_kannada": name_kannada,
                "phone": phone,
                "address": address,
                "address_kannada": address_kannada,
                "opening_balance": opening_balance,
                "balance_type": balance_type,
                "ifsc": ifsc,
                "account_no": account_no,
                "bank_name": bank_name,
                "branch_name": branch_name,
            }
            return render(request, "accounts/add_farmer.html", {"farmer": farmer, "form_values": form_values})

        farmer.name = name
        farmer.name_kannada = name_kannada
        farmer.phone = phone
        farmer.address = address
        farmer.address_kannada = address_kannada

        farmer.opening_balance = opening_balance
        farmer.balance_type = balance_type
        farmer.ifsc = ifsc
        farmer.account_no = account_no
        farmer.bank_name = bank_name
        farmer.branch_name = branch_name

        farmer.save()
        messages.success(request, "Farmer updated successfully.")
        return redirect("farmer_list")

    return render(request, "accounts/add_farmer.html", {"farmer": farmer})


@login_required
def delete_farmer(request, farmer_id):
    try:
        farmer = Farmer.objects.get(id=farmer_id)
        farmer.delete()
        messages.success(request, "Farmer deleted successfully.")
    except Exception as e:
        messages.error(request, f"Error deleting farmer: {str(e)}")
    return redirect("farmer_list")


@login_required
def trader_list(request):
    selected_date = _parse_account_date(request)
    date_str = selected_date.isoformat()

    traders = Trader.objects.filter(created_at__date=selected_date).order_by("-created_at")
    q = request.GET.get("q", "").strip()
    if q:
        traders = traders.filter(
            Q(name__icontains=q) | Q(name_kannada__icontains=q) | Q(phone__icontains=q)
        )

    form_values = {}

    if request.method == "POST":
        name = request.POST.get("full_name", "").strip()
        name_kannada = request.POST.get("name_kannada", "").strip()
        phone = request.POST.get("contact", "").strip()
        address = request.POST.get("address", "").strip()
        address_kannada = request.POST.get("address_kannada", "").strip()
        opening_balance = request.POST.get("opening_balance") or 0
        balance_type = request.POST.get("balance_type")
        ifsc = request.POST.get("ifsc", "").strip()
        account_no = request.POST.get("account_no", "").strip()
        bank_name = request.POST.get("bank_name", "").strip()
        branch_name = request.POST.get("branch_name", "").strip()
        short_code = request.POST.get("short_code", "").strip()
        pan = request.POST.get("pan", "").strip()
        gstin = request.POST.get("gstin", "").strip()
        mobile_no = request.POST.get("mobile_no", "").strip()
        email = request.POST.get("email", "").strip()
        pin = request.POST.get("pin", "").strip()

        form_values = {
            "name": name,
            "name_kannada": name_kannada,
            "phone": phone,
            "address": address,
            "address_kannada": address_kannada,
            "opening_balance": opening_balance,
            "balance_type": balance_type,
            "ifsc": ifsc,
            "account_no": account_no,
            "bank_name": bank_name,
            "branch_name": branch_name,
            "short_code": short_code,
            "pan": pan,
            "gstin": gstin,
            "mobile_no": mobile_no,
            "email": email,
            "pin": pin,
        }

        if not name or not phone:
            messages.error(request, "Name and contact number are required.")
        else:
            Trader.objects.create(
                name=name,
                name_kannada=name_kannada,
                phone=phone,
                address=address,
                address_kannada=address_kannada,
                opening_balance=opening_balance,
                balance_type=balance_type,
                ifsc=ifsc,
                account_no=account_no,
                bank_name=bank_name,
                branch_name=branch_name,
                short_code=short_code,
                pan=pan,
                gstin=gstin,
                mobile_no=mobile_no,
                email=email,
                pin=pin or "581106",
            )
            messages.success(request, "Trader added successfully.")
            return redirect(f"{reverse('trader_list')}?date={date_str}")

    show_form = request.GET.get("add") == "1" or request.method == "POST" or bool(form_values)

    return render(request, "accounts/trader_list.html", {
        "date_value": date_str,
        "traders": traders,
        "form_values": form_values,
        "q": q,
        "show_form": show_form,
    })


@login_required
def add_trader(request):
    if request.method == "POST":
        name = request.POST.get("full_name")
        name_kannada = request.POST.get("name_kannada")
        phone = request.POST.get("contact")
        address = request.POST.get("address")
        address_kannada = request.POST.get("address_kannada")

        # Bank Details
        opening_balance = request.POST.get("opening_balance") or 0
        balance_type = request.POST.get("balance_type")
        ifsc = request.POST.get("ifsc")
        account_no = request.POST.get("account_no")
        bank_name = request.POST.get("bank_name")
        branch_name = request.POST.get("branch_name")

        # Additional Trader Details
        short_code = request.POST.get("short_code")
        pan = request.POST.get("pan")
        gstin = request.POST.get("gstin")
        mobile_no = request.POST.get("mobile_no")
        email = request.POST.get("email")
        pin = request.POST.get("pin")

        Trader.objects.create(
            name=name,
            name_kannada=name_kannada,
            phone=phone,
            address=address,
            address_kannada=address_kannada,
            opening_balance=opening_balance,
            balance_type=balance_type,
            ifsc=ifsc,
            account_no=account_no,
            bank_name=bank_name,
            branch_name=branch_name,
            short_code=short_code,
            pan=pan,
            gstin=gstin,
            mobile_no=mobile_no,
            email=email,
            pin=pin,
        )
        messages.success(request, "Trader added successfully.")
        return redirect("trader_list")

    return redirect(f"{reverse('trader_list')}?add=1")


@login_required
def edit_trader(request, trader_id):
    trader = Trader.objects.get(id=trader_id)
    if request.method == "POST":
        trader.name = request.POST.get("full_name")
        trader.name_kannada = request.POST.get("name_kannada")
        trader.phone = request.POST.get("contact")
        trader.address = request.POST.get("address")
        trader.address_kannada = request.POST.get("address_kannada")

        trader.opening_balance = request.POST.get("opening_balance") or 0
        trader.balance_type = request.POST.get("balance_type")
        trader.ifsc = request.POST.get("ifsc")
        trader.account_no = request.POST.get("account_no")
        trader.bank_name = request.POST.get("bank_name")
        trader.branch_name = request.POST.get("branch_name")

        trader.short_code = request.POST.get("short_code")
        trader.pan = request.POST.get("pan")
        trader.gstin = request.POST.get("gstin")
        trader.mobile_no = request.POST.get("mobile_no")
        trader.email = request.POST.get("email")
        trader.pin = request.POST.get("pin")

        trader.save()
        messages.success(request, "Trader updated successfully.")
        return redirect("trader_list")

    return render(request, "accounts/add_trader.html", {"trader": trader})


@login_required
def delete_trader(request, trader_id):
    try:
        trader = Trader.objects.get(id=trader_id)
        trader.delete()
        messages.success(request, "Trader deleted successfully.")
    except Exception as e:
        messages.error(request, f"Error deleting trader: {str(e)}")
    return redirect("trader_list")


def _avak_to_json(avak):
    farmer = avak.farmer if hasattr(avak, "farmer") and avak.farmer else None
    buyer = avak.buyer if hasattr(avak, "buyer") and avak.buyer else None
    return {
        "id": avak.id,
        "date": str(avak.date),
        "place": avak.place or "",
        "farmer_id": avak.farmer_id,
        "farmer_name": farmer.name if farmer else "",
        "farmer_name_kannada": farmer.name_kannada if farmer else "",
        "lot_number": avak.lot_number,
        "variety": avak.variety or "",
        "no_of_bags": avak.no_of_bags,
        "hamali_rate": str(avak.hamali_rate),
        "hamali_total": str(avak.hamali_total),
        "freight": str(avak.freight),
        "empty_bags": avak.empty_bags,
        "advance": str(avak.advance),
        "rate": str(avak.rate or 0),
        "buyer_id": avak.buyer_id or "",
        "buyer_code": (buyer.short_code or buyer.name) if buyer else "",
    }


def _avak_tender_fields(avak):
    buyer_code = ""
    buyer_id = ""
    if avak.buyer:
        buyer_code = avak.buyer.short_code or avak.buyer.name
        buyer_id = avak.buyer_id
    return float(avak.rate or 0), buyer_code, buyer_id


def _avak_totals_for_date(entry_date):
    avaks = Avak.objects.filter(date=entry_date, is_cancelled=False)
    return {
        "bags": sum(a.no_of_bags for a in avaks),
        "freight": float(sum(a.freight for a in avaks)),
        "empty_bags": sum(a.empty_bags for a in avaks),
        "advance": float(sum(a.advance for a in avaks)),
        "hamali": float(sum(a.hamali_total for a in avaks)),
    }


def _avak_ajax_success(avak, message, totals_date=None):
    td = totals_date or avak.date
    return JsonResponse(
        {
            "success": True,
            "message": message,
            "avak": _avak_to_json(avak),
            "totals": _avak_totals_for_date(td),
        }
    )


@login_required
def avak_list(request):
    date_param = request.GET.get("date")
    if date_param:
        try:
            today = date.fromisoformat(date_param)
        except ValueError:
            today = date.today()
    else:
        today = date.today()

    from django.db.models.functions import Length

    avaks = Avak.objects.filter(date=today, is_cancelled=False).order_by(Length("lot_number"), "lot_number")

    q = request.GET.get("q")
    if q:
        avaks = Avak.objects.filter(
            Q(date=today) & Q(is_cancelled=False)
            & (
                Q(farmer__name__icontains=q)
                | Q(lot_number__icontains=q)
                | Q(place__icontains=q)
            )
        ).order_by(Length("lot_number"), "lot_number")

    # Smallest available unused lot number logic: Find the smallest positive integer starting from 1 that is not currently in use
    active_lots = set()
    digit_lots = Avak.objects.filter(date=today, lot_number__regex=r"^\d+$", is_cancelled=False)
    for l in digit_lots:
        if l.lot_number.isdigit():
            active_lots.add(int(l.lot_number))
    next_lot = 1
    while next_lot in active_lots:
        next_lot += 1

    form_values = {
        "date": today.strftime("%Y-%m-%d"),
        "place": "",
        "lot_number": str(next_lot),
        "no_of_bags": "",
        "hamali_rate": "3.70",
        "hamali_total": "0.00",
        "freight": "0.00",
        "empty_bags": 0,
        "advance": "0.00",
        "buyer_id": "",
    }

    # Totals for the table footer
    totals = {
        "bags": sum(a.no_of_bags for a in avaks),
        "freight": sum(a.freight for a in avaks),
        "empty_bags": sum(a.empty_bags for a in avaks),
        "advance": sum(a.advance for a in avaks),
        "hamali": sum(a.hamali_total for a in avaks),
    }

    return render(
        request,
        "accounts/avak_list.html",
        {
            "avaks": avaks,
            "form_values": form_values,
            "totals": totals,
            "date_value": today.strftime("%Y-%m-%d"),
        },
    )


@login_required
def add_avak(request):
    if request.method == "POST":
        entry_date = (request.POST.get("date") or "").strip()
        farmer_id = (request.POST.get("farmer_id") or "").strip()
        place = (request.POST.get("place") or "").strip()
        lot_number = (request.POST.get("lot_number") or "").strip()
        variety = None

        no_of_bags = _to_int(request.POST.get("no_of_bags"), default=0)
        hamali_rate = _to_decimal(request.POST.get("hamali_rate"), default="0")
        hamali_total = _to_decimal(request.POST.get("hamali_total"), default="0")

        freight = _to_decimal(request.POST.get("freight"), default="0")
        empty_bags = _to_int(request.POST.get("empty_bags"), default=0)
        advance = _to_decimal(request.POST.get("advance"), default="0")
        buyer_id = request.POST.get("buyer_id")

        selected_farmer = None
        if farmer_id.isdigit():
            selected_farmer = Farmer.objects.filter(id=int(farmer_id)).first()

        form_values = {
            "date": entry_date,
            "farmer_id": farmer_id,
            "place": place,
            "lot_number": lot_number,
            "no_of_bags": no_of_bags,
            "hamali_rate": str(hamali_rate),
            "hamali_total": str(hamali_total),
            "freight": str(freight),
            "empty_bags": empty_bags,
            "advance": str(advance),
        }

        lot_number_error = None

        has_error = False
        if not entry_date:
            messages.error(request, "Date is required.")
            has_error = True
        if not farmer_id:
            messages.error(request, "Farmer is required.")
            has_error = True
        elif not farmer_id.isdigit() or not selected_farmer:
            messages.error(request, "Please select a valid farmer.")
            has_error = True
        if not lot_number:
            messages.error(request, "Lot number is required.")
            has_error = True
        if no_of_bags < 1:
            messages.error(request, "Bags must be at least 1.")
            has_error = True

        if lot_number and entry_date:
            duplicate = Avak.objects.filter(
                lot_number__iexact=lot_number, date=entry_date, is_cancelled=False
            ).exists()
            if duplicate:
                lot_number_error = (
                    "Lot number already exists. Please use a different lot number."
                )
                has_error = True

        if has_error:
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                error_msg = (
                    lot_number_error
                    or ("Bags must be at least 1." if no_of_bags < 1 else None)
                    or "Please check all required fields."
                )
                return JsonResponse({"success": False, "error": error_msg})
            today = date.today()
            return render(
                request,
                "accounts/add_avak.html",
                {
                    "form_action": reverse("add_avak"),
                    "date_value": today.strftime("%Y-%m-%d"),
                    "form_values": form_values,
                    "selected_farmer": selected_farmer,
                    "is_edit": False,
                    "lot_number_error": lot_number_error,
                },
            )

        try:
            avak_obj = None
            # Check if a soft-deleted lot with this number already exists
            deleted_lot = Avak.objects.filter(
                lot_number__iexact=lot_number, date=entry_date, is_cancelled=True
            ).first()

            if deleted_lot:
                # Restore and update the existing soft-deleted lot
                deleted_lot.farmer_id = int(farmer_id)
                deleted_lot.place = place
                deleted_lot.variety = variety
                deleted_lot.no_of_bags = no_of_bags
                deleted_lot.hamali_rate = hamali_rate
                deleted_lot.hamali_total = hamali_total
                deleted_lot.freight = freight
                deleted_lot.empty_bags = empty_bags
                deleted_lot.advance = advance
                deleted_lot.buyer_id = buyer_id if buyer_id else None
                deleted_lot.is_cancelled = False
                deleted_lot.save()
                avak_obj = deleted_lot
            else:
                # Create a completely new lot
                avak_obj = Avak.objects.create(
                    date=entry_date,
                    farmer_id=int(farmer_id),
                    place=place,
                    lot_number=lot_number,
                    variety=variety,
                    no_of_bags=no_of_bags,
                    hamali_rate=hamali_rate,
                    hamali_total=hamali_total,
                    freight=freight,
                    empty_bags=empty_bags,
                    advance=advance,
                    buyer_id=buyer_id if buyer_id else None,
                )
        except IntegrityError:
            lot_number_error = (
                "Lot number already exists. Please use a different lot number."
            )
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"success": False, "error": lot_number_error})
            today = date.today()
            return render(
                request,
                "accounts/add_avak.html",
                {
                    "form_action": reverse("add_avak"),
                    "date_value": today.strftime("%Y-%m-%d"),
                    "form_values": form_values,
                    "selected_farmer": selected_farmer,
                    "is_edit": False,
                    "lot_number_error": lot_number_error,
                },
            )

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            list_date = (request.POST.get("list_date") or entry_date).strip()
            return _avak_ajax_success(
                avak_obj, "Avak entry added successfully.", totals_date=list_date
            )

        messages.success(request, "Avak entry added successfully.")
        return redirect(f"{reverse('avak_list')}?date={entry_date}")

    today = date.today()

    # Calculate smallest unused lot number for today
    active_lots = set()
    digit_lots = Avak.objects.filter(date=today, lot_number__regex=r"^\d+$", is_cancelled=False)
    for l in digit_lots:
        if l.lot_number.isdigit():
            active_lots.add(int(l.lot_number))
    next_lot = 1
    while next_lot in active_lots:
        next_lot += 1

    # Fetch hamali rate from market rates (closest preceding or today's rate)
    market_rate = MarketRate.objects.filter(date__lte=today).order_by("-date").first()
    default_hamali_rate = str(market_rate.farmer_unloading_per_bag) if market_rate else "3.70"

    return render(
        request,
        "accounts/add_avak.html",
        {
            "form_action": reverse("add_avak"),
            "date_value": today.strftime("%Y-%m-%d"),
            "form_values": {
                "date": today.strftime("%Y-%m-%d"),
                "place": "",
                "lot_number": str(next_lot),
                "no_of_bags": 0,
                "hamali_rate": default_hamali_rate,
                "hamali_total": "0.00",
                "freight": "0.00",
                "empty_bags": 0,
                "advance": "0.00",
                "buyer_id": "",
            },
            "selected_farmer": None,
            "selected_buyer": None,
            "is_edit": False,
            "lot_number_error": None,
        },
    )


@login_required
def edit_avak(request, avak_id):
    avak = Avak.objects.get(id=avak_id)
    if request.method == "POST":
        entry_date = (request.POST.get("date") or "").strip()
        farmer_id = (request.POST.get("farmer_id") or "").strip()
        place = (request.POST.get("place") or "").strip()
        lot_number = (request.POST.get("lot_number") or "").strip()

        no_of_bags = _to_int(request.POST.get("no_of_bags"), default=0)
        hamali_rate = _to_decimal(request.POST.get("hamali_rate"), default="0")
        hamali_total = _to_decimal(request.POST.get("hamali_total"), default="0")

        freight = _to_decimal(request.POST.get("freight"), default="0")
        empty_bags = _to_int(request.POST.get("empty_bags"), default=0)
        advance = _to_decimal(request.POST.get("advance"), default="0")
        buyer_id = request.POST.get("buyer_id")

        selected_farmer = None
        if farmer_id.isdigit():
            selected_farmer = Farmer.objects.filter(id=int(farmer_id)).first()

        form_values = {
            "date": entry_date,
            "farmer_id": farmer_id,
            "place": place,
            "lot_number": lot_number,
            "no_of_bags": no_of_bags,
            "hamali_rate": str(hamali_rate),
            "hamali_total": str(hamali_total),
            "freight": str(freight),
            "empty_bags": empty_bags,
            "advance": str(advance),
        }

        lot_number_error = None

        has_error = False
        if not entry_date:
            messages.error(request, "Date is required.")
            has_error = True
        if not farmer_id:
            messages.error(request, "Farmer is required.")
            has_error = True
        elif not farmer_id.isdigit() or not selected_farmer:
            messages.error(request, "Please select a valid farmer.")
            has_error = True
        if not lot_number:
            messages.error(request, "Lot number is required.")
            has_error = True
        if no_of_bags < 1:
            messages.error(request, "Bags must be at least 1.")
            has_error = True

        if lot_number and entry_date:
            duplicate = (
                Avak.objects.filter(lot_number__iexact=lot_number, date=entry_date, is_cancelled=False)
                .exclude(id=avak.id)
                .exists()
            )
            if duplicate:
                lot_number_error = (
                    "Lot number already exists. Please use a different lot number."
                )
                has_error = True

        if has_error:
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                error_msg = (
                    lot_number_error
                    or ("Bags must be at least 1." if no_of_bags < 1 else None)
                    or "Please check all required fields."
                )
                return JsonResponse({"success": False, "error": error_msg})
            return render(
                request,
                "accounts/add_avak.html",
                {
                    "avak": avak,
                    "form_action": reverse("edit_avak", args=[avak.id]),
                    "date_value": entry_date or avak.date.strftime("%Y-%m-%d"),
                    "form_values": form_values,
                    "selected_farmer": selected_farmer,
                    "is_edit": True,
                    "lot_number_error": lot_number_error,
                },
            )

        avak.date = entry_date
        avak.farmer_id = int(farmer_id)
        avak.place = place
        avak.lot_number = lot_number
        avak.variety = None
        avak.no_of_bags = no_of_bags
        avak.hamali_rate = hamali_rate
        avak.hamali_total = hamali_total
        avak.freight = freight
        avak.empty_bags = empty_bags
        avak.advance = advance
        if "buyer_id" in request.POST:
            avak.buyer_id = buyer_id if buyer_id else None

        try:
            # Check if a soft-deleted lot with this new number already exists
            deleted_lot = Avak.objects.filter(
                lot_number__iexact=lot_number, date=entry_date, is_cancelled=True
            ).exclude(id=avak.id).first()
            if deleted_lot:
                # We want to use this number, so permanently delete the old soft-deleted entry
                deleted_lot.delete()

            avak.save()
        except IntegrityError:
            lot_number_error = (
                "Lot number already exists. Please use a different lot number."
            )
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"success": False, "error": lot_number_error})
            return render(
                request,
                "accounts/add_avak.html",
                {
                    "avak": avak,
                    "form_action": reverse("edit_avak", args=[avak.id]),
                    "date_value": entry_date or avak.date.strftime("%Y-%m-%d"),
                    "form_values": form_values,
                    "selected_farmer": selected_farmer,
                    "is_edit": True,
                    "lot_number_error": lot_number_error,
                },
            )

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            list_date = (request.POST.get("list_date") or entry_date).strip()
            return _avak_ajax_success(
                avak, "Avak entry updated successfully.", totals_date=list_date
            )

        messages.success(request, "Avak entry updated successfully.")
        return redirect(f"{reverse('avak_list')}?date={avak.date}")

    return render(
        request,
        "accounts/add_avak.html",
        {
            "avak": avak,
            "form_action": reverse("edit_avak", args=[avak.id]),
            "date_value": avak.date.strftime("%Y-%m-%d"),
            "form_values": {
                "date": avak.date.strftime("%Y-%m-%d"),
                "place": avak.place or "",
                "lot_number": avak.lot_number or "",
                "no_of_bags": avak.no_of_bags,
                "hamali_rate": str(avak.hamali_rate),
                "hamali_total": str(avak.hamali_total),
                "freight": str(avak.freight),
                "empty_bags": avak.empty_bags,
                "advance": str(avak.advance),
                "buyer_id": avak.buyer_id,
            },
            "selected_farmer": avak.farmer,
            "selected_buyer": avak.buyer,
            "is_edit": True,
            "lot_number_error": None,
        },
    )


@login_required
def delete_avak(request, avak_id):
    try:
        avak = Avak.objects.get(id=avak_id)
        if avak.bikri_entries.filter(is_cancelled=False).exists():
            messages.error(request, f"Lot {avak.lot_number} already has Vikri entry. Cannot delete. You can only edit.")
        else:
            avak.is_cancelled = True
            avak.save()
            messages.success(request, "Avak entry deleted successfully.")
    except Exception as e:
        messages.error(request, f"Error deleting avak entry: {str(e)}")
    return redirect("avak_list")


@login_required
def view_avak(request, avak_id):
    from .models import Avak

    avak = Avak.objects.get(id=avak_id)
    # Prepare data for dashes: 10 columns per row
    num_bags = avak.no_of_bags
    num_rows = (num_bags + 9) // 10  # Ceiling division

    # We'll pass a list of rows, each containing a range of col indices
    rows = []
    for r in range(num_rows):
        start = r * 10
        end = min((r + 1) * 10, num_bags)
        rows.append(range(start, end))

    # Linked vikri (bikri) entries for this avak
    bikri_entries = avak.bikri_entries.filter(is_cancelled=False).order_by('bill_no')

    return render(
        request,
        "accounts/view_avak.html",
        {"avak": avak, "rows": rows, "total_bags": num_bags, "bikri_entries": bikri_entries},
    )


@login_required
def view_all_avak(request):
    date_param = request.GET.get("date")
    if date_param:
        try:
            today = date.fromisoformat(date_param)
        except ValueError:
            today = date.today()
    else:
        today = date.today()

    from django.db.models.functions import Length

    avaks = Avak.objects.filter(date=today, is_cancelled=False).select_related(
        "farmer", "buyer"
    ).order_by(Length("lot_number"), "lot_number")

    # Prepare bag dash rows (10 columns per row, padded)
    total_bags = 0
    for avak in avaks:
        num_bags = int(avak.no_of_bags or 0)
        total_bags += num_bags
        num_rows = (num_bags + 9) // 10 if num_bags else 0
        rows = []
        for r in range(num_rows):
            start = r * 10
            end = min((r + 1) * 10, num_bags)
            cells = [i + 1 for i in range(start, end)]
            while len(cells) < 10:
                cells.append(0)
            rows.append(cells)
        avak.rows_list = rows

    return render(
        request,
        "accounts/view_all_avak.html",
        {
            "avaks": avaks,
            "date_instance": today,
            "total_bags": total_bags,
            "total_lots": avaks.count(),
            "auto_print": request.GET.get("print") == "1",
        },
    )


def _prepare_avak_bag_rows(avak):
    num_bags = int(avak.no_of_bags or 0)
    num_rows = (num_bags + 9) // 10 if num_bags else 0
    rows = []
    for r in range(num_rows):
        start = r * 10
        end = min((r + 1) * 10, num_bags)
        cells = [i + 1 for i in range(start, end)]
        while len(cells) < 10:
            cells.append(0)
        rows.append(cells)
    return rows


def _filter_avaks_for_thookada(selected, from_lot, to_lot):
    from django.db.models.functions import Length as DbLength

    avaks = (
        Avak.objects.filter(date=selected, is_cancelled=False)
        .select_related("farmer", "buyer")
        .order_by(DbLength("lot_number"), "lot_number")
    )
    from_int = int(from_lot) if str(from_lot).isdigit() else 1
    to_int = int(to_lot) if str(to_lot).isdigit() else None
    filtered = []
    for avak in avaks:
        if avak.lot_number.isdigit():
            lot_num = int(avak.lot_number)
            if lot_num < from_int:
                continue
            if to_int and lot_num > to_int:
                continue
        filtered.append(avak)
    return filtered


@login_required
def thookada_chopada(request):
    date_param = request.GET.get("date")
    if date_param:
        try:
            selected = date.fromisoformat(date_param)
        except ValueError:
            selected = date.today()
    else:
        selected = date.today()
    return render(
        request,
        "accounts/thookada_chopada.html",
        {"today": selected.strftime("%Y-%m-%d")},
    )


@login_required
def thookada_report(request):
    mode = request.GET.get("mode", "lot")
    if mode not in ("lot", "farmer", "bill_single", "bill_double"):
        mode = "lot"

    date_param = request.GET.get("date")
    if date_param:
        try:
            selected = date.fromisoformat(date_param)
        except ValueError:
            selected = date.today()
    else:
        selected = date.today()

    from_lot = request.GET.get("from_lot", "1")
    to_lot = request.GET.get("to_lot", "")
    mark_needed = request.GET.get("mark_needed") == "1"

    avaks = _filter_avaks_for_thookada(selected, from_lot, to_lot)
    for avak in avaks:
        avak.rows_list = _prepare_avak_bag_rows(avak)

    total_bags = sum(int(a.no_of_bags or 0) for a in avaks)
    farmer_groups = []
    if mode == "farmer":
        groups = {}
        for avak in avaks:
            groups.setdefault(avak.farmer_id, {"farmer": avak.farmer, "avaks": []})[
                "avaks"
            ].append(avak)
        farmer_groups = sorted(groups.values(), key=lambda g: g["farmer"].name or "")

    avak_pairs = []
    if mode == "bill_double":
        avak_pairs = [avaks[i : i + 2] for i in range(0, len(avaks), 2)]

    mode_titles = {
        "lot": "ಲಾಟ್ ಪ್ರಕಾರ (Lot Prakara)",
        "farmer": "ರೈತರ ಪ್ರಕಾರ (Rythara Prakara)",
        "bill_single": "ತೂಕದ ಬಿಲ್ ಸಿಂಗಲ್ (Thookada Bill Single)",
        "bill_double": "ತೂಕದ ಬಿಲ್ ಡಬಲ್ (Thookada Bill Double)",
    }

    return render(
        request,
        "accounts/thookada_report.html",
        {
            "mode": mode,
            "mode_title": mode_titles[mode],
            "date_instance": selected,
            "from_lot": from_lot,
            "to_lot": to_lot,
            "mark_needed": mark_needed,
            "avaks": avaks,
            "farmer_groups": farmer_groups,
            "avak_pairs": avak_pairs,
            "total_bags": total_bags,
            "total_lots": len(avaks),
            "auto_print": request.GET.get("print") == "1",
        },
    )


from django.http import JsonResponse


@login_required
def get_farmers(request):
    q = request.GET.get("q", "")
    place = request.GET.get("place", "")

    # Strictly filter by place if provided, otherwise return empty results to ensure place-wise consistency
    if not place:
        return JsonResponse({"results": []})

    farmers = Farmer.objects.filter(
        Q(address__icontains=place) | Q(address_kannada__icontains=place)
    )

    if q:
        farmers = farmers.filter(Q(name__icontains=q) | Q(name_kannada__icontains=q))

    results = []
    for f in farmers[:20]:
        display_text = f.name
        if f.name_kannada:
            display_text += f" ({f.name_kannada})"

        results.append({"id": f.id, "text": display_text})

    return JsonResponse({"results": results})


@login_required
def get_traders(request):
    q = request.GET.get("q", "")
    traders = Trader.objects.all().order_by("name")
    
    date_param = request.GET.get("date")
    if date_param:
        bikri_ids = Bikri.objects.filter(
            date=date_param, is_cancelled=False
        ).values_list("buyer_id", flat=True).distinct()
        bill_ids = TraderBill.objects.filter(
            date=date_param
        ).values_list("buyer_id", flat=True).distinct()
        trader_ids = set(list(bikri_ids) + list(bill_ids))
        traders = traders.filter(id__in=trader_ids)

    if q:
        traders = traders.filter(Q(name__icontains=q) | Q(short_code__icontains=q))
    
    results = []
    # Return top 30 traders
    for t in traders[:30]:
        display_text = f"{t.short_code or t.name} ({t.name})"
        results.append({"id": t.id, "text": display_text})
    
    return JsonResponse({"results": results})


@login_required
def get_places(request):
    q = request.GET.get("q", "")
    # Get unique addresses/places from Farmer model
    places = (
        Farmer.objects.filter(Q(address__icontains=q) | Q(address_kannada__icontains=q))
        .values_list("address", flat=True)
        .distinct()
    )

    # Also consider places from Avak model if they might be different
    avak_places = (
        Avak.objects.filter(place__icontains=q)
        .values_list("place", flat=True)
        .distinct()
    )

    all_places = set(list(places) + list(avak_places))
    results = [{"id": p, "text": p} for p in sorted(all_places) if p][:20]

    return JsonResponse({"results": results})


def _collect_farmer_bikri_groups(filtered_avaks):
    """Group active bikri rows by farmer for chopada (matches view_bikri grouping)."""
    from collections import OrderedDict

    farmer_groups = OrderedDict()
    for avak in filtered_avaks:
        if avak.is_cancelled:
            continue
        fid = avak.farmer_id
        if fid not in farmer_groups:
            farmer_groups[fid] = {
                "farmer_name": avak.farmer.name,
                "bikris": [],
            }
        for b in avak.bikri_entries.all():
            if b.is_cancelled:
                continue
            farmer_groups[fid]["bikris"].append(b)
    return farmer_groups


def _chopada_lot_row(avak, breakdown_row):
    b = breakdown_row["bikri"]
    bill_no = (b.bill_no or "").strip() or "-"
    if bill_no == "-" and hasattr(b, "traderbillitem"):
        try:
            bill_no = b.traderbillitem.bill.invoice_no
        except Exception:
            pass
    return {
        "lot_no": avak.lot_number,
        "avak_bags": avak.no_of_bags,
        "bags": b.no_of_bags,
        "weight": float(b.total_weight),
        "rate": float(b.rate),
        "trader": b.buyer.short_code or b.buyer.name if b.buyer else "-",
        "net_payable": float(breakdown_row["net_payable"]),
        "amount": float(b.amount),
        "deduction": float(breakdown_row["deduction"]),
        "bill_no": bill_no,
        "bag_weights": [float(w.weight) for w in b.weights.all().order_by("bag_no")],
    }


@login_required
def chopada(request):
    from django.db.models.functions import Length as DbLength

    date_param = request.GET.get("date")
    mode = request.GET.get("mode", "bastani")  # bastani | poorna | registered
    from_lot = request.GET.get("from_lot", "1")
    to_lot = request.GET.get("to_lot", "")
    farmer_id = request.GET.get("farmer_id", "")
    do_print = bool(request.GET.get("print"))

    if date_param:
        try:
            selected = date.fromisoformat(date_param)
        except ValueError:
            selected = date.today()
    else:
        selected = date.today()

    selected_str = selected.strftime("%Y-%m-%d")

    farmers_all = Farmer.objects.order_by("name")

    print_data = None
    if do_print:
        avaks = (
            Avak.objects.filter(date=selected)
            .select_related("farmer")
            .prefetch_related("bikri_entries__buyer", "bikri_entries__traderbillitem__bill")
            .order_by(DbLength("lot_number"), "lot_number")
        )

        # Apply numeric lot-range filter
        from_int = int(from_lot) if str(from_lot).isdigit() else 1
        to_int = int(to_lot) if str(to_lot).isdigit() else None

        filtered = []
        for a in avaks:
            if a.lot_number.isdigit():
                n = int(a.lot_number)
                if n < from_int:
                    continue
                if to_int and n > to_int:
                    continue
            if farmer_id and str(farmer_id).isdigit() and a.farmer_id != int(farmer_id):
                continue
            filtered.append(a)

        if mode == "bastani":
            rows = []
            for data in _collect_farmer_bikri_groups(filtered).values():
                _, breakdown = _calc_group_net_payable_breakdown(data["bikris"], selected)
                for row in breakdown:
                    b = row["bikri"]
                    avak = b.avak
                    rows.append(
                        {
                            "lot_no": avak.lot_number,
                            "farmer_name": data["farmer_name"],
                            "bags": b.no_of_bags,
                            "weight": float(b.total_weight),
                            "rate": float(b.rate),
                            "trader": (
                                b.buyer.short_code or b.buyer.name if b.buyer else "-"
                            ),
                            "net_payable": float(row["net_payable"]),
                            "amount": float(b.amount),
                        }
                    )
            rows.sort(key=lambda r: _lot_number_sort_key(r["lot_no"]))
            print_data = rows

        elif mode == "poorna":
            print_data = []
            for data in _collect_farmer_bikri_groups(filtered).values():
                lots = []
                _, breakdown = _calc_group_net_payable_breakdown(data["bikris"], selected)
                for row in breakdown:
                    lots.append(_chopada_lot_row(row["bikri"].avak, row))
                print_data.append(
                    {
                        "farmer_name": data["farmer_name"],
                        "lots": lots,
                    }
                )

        elif mode == "registered":
            active_lots = []
            deleted_lots = []
            for a in filtered:
                lot_info = {
                    "lot_no": a.lot_number,
                    "farmer": a.farmer.name,
                    "bags": a.no_of_bags,
                    "place": a.place or a.farmer.address or "",
                }
                if a.is_cancelled:
                    deleted_lots.append(lot_info)
                else:
                    active_lots.append(lot_info)
            print_data = {
                "active": active_lots,
                "deleted": deleted_lots,
            }

    return render(
        request,
        "accounts/chopada.html",
        {
            "selected_date": selected_str,
            "selected_date_display": selected.strftime("%d-%m-%Y"),
            "mode": mode,
            "from_lot": from_lot,
            "to_lot": to_lot,
            "farmer_id": farmer_id,
            "farmers": farmers_all,
            "print_data": json.dumps(print_data) if print_data is not None else "null",
            "do_print": do_print,
        },
    )


@login_required
def gst_reports(request):
    system_name = "MSBC-2025-26"  # Example title from image
    context = {
        "system_name": system_name,
        "from_date": date.today().replace(day=1).strftime("%Y-%m-%d"),
        "to_date": date.today().strftime("%Y-%m-%d"),
    }
    return render(request, "accounts/gst_reports.html", context)


@login_required
def monthwise_gst_report(request):
    from django.db.models.functions import TruncMonth
    from django.db.models import Sum
    
    from_date = request.GET.get('from_date')
    to_date = request.GET.get('to_date')
    
    if not (from_date and to_date):
        return redirect('gst_reports')
    
    from_date_obj = date.fromisoformat(from_date)
    to_date_obj = date.fromisoformat(to_date)
    
    bills = TraderBill.objects.filter(
        date__range=[from_date, to_date]
    ).annotate(
        month=TruncMonth('date')
    ).values('month').annotate(
        total_weight=Sum('total_weight'),
        bastani=Sum('total_amount'), # total_amount in model seems to be 'Bastani' (original amount)
        packing_total=Sum('packing'),
        hamali_total=Sum('hamali'),
        wm_fee_total=Sum('weighman_fee'),
        commission_total=Sum('commission'),
        cess_total=Sum('cess'),
        gst_total=Sum('gst'),
        round_off_total=Sum('round_off'),
        grand_total_sum=Sum('grand_total'),
    ).order_by('month')
    
    report_data = []
    for b in bills:
        taxable_amount = (
            (b['bastani'] or 0) + 
            (b['packing_total'] or 0) + 
            (b['hamali_total'] or 0) + 
            (b['wm_fee_total'] or 0) + 
            (b['commission_total'] or 0) + 
            (b['cess_total'] or 0)
        )
        report_data.append({
            'month_display': b['month'].strftime('%b-%y'),
            'weight': b['total_weight'],
            'bastani': b['bastani'],
            'packing': b['packing_total'],
            'hamali': b['hamali_total'],
            'wm_fee': b['wm_fee_total'],
            'commission': b['commission_total'],
            'cess': b['cess_total'],
            'taxable_amount': taxable_amount,
            'sgst': (b['gst_total'] or 0) / 2,
            'cgst': (b['gst_total'] or 0) / 2,
            'round_off': b['round_off_total'],
            'grand_total': b['grand_total_sum'],
        })
    
    context = {
        'from_date': from_date_obj,
        'to_date': to_date_obj,
        'report_data': report_data,
        'totals': {
            'weight': sum(r['weight'] or 0 for r in report_data),
            'bastani': sum(r['bastani'] or 0 for r in report_data),
            'packing': sum(r['packing'] or 0 for r in report_data),
            'hamali': sum(r['hamali'] or 0 for r in report_data),
            'wm_fee': sum(r['wm_fee'] or 0 for r in report_data),
            'commission': sum(r['commission'] or 0 for r in report_data),
            'cess': sum(r['cess'] or 0 for r in report_data),
            'taxable_amount': sum(r['taxable_amount'] or 0 for r in report_data),
            'sgst': sum(r['sgst'] or 0 for r in report_data),
            'cgst': sum(r['cgst'] or 0 for r in report_data),
            'round_off': sum(r['round_off'] or 0 for r in report_data),
            'grand_total': sum(r['grand_total'] or 0 for r in report_data),
        },
        'system_name': "MSBC-2025-26",
        'auto_print': request.GET.get('print') == '1',
    }
    return render(request, "accounts/reports/monthwise_gst.html", context)


@login_required
def detailed_gst_report(request):
    from_date = request.GET.get('from_date')
    to_date = request.GET.get('to_date')
    
    if not (from_date and to_date):
        return redirect('gst_reports')
    
    from_date_obj = date.fromisoformat(from_date)
    to_date_obj = date.fromisoformat(to_date)
    
    bills = TraderBill.objects.filter(
        date__range=[from_date, to_date]
    ).select_related('buyer').order_by('date', 'invoice_no')
    
    report_data = []
    for b in bills:
        taxable_amount = (
            (b.total_amount or 0) + 
            (b.packing or 0) + 
            (b.hamali or 0) + 
            (b.weighman_fee or 0) + 
            (b.commission or 0) + 
            (b.cess or 0)
        )
        report_data.append({
            'date': b.date,
            'bill_no': b.invoice_no,
            'name': b.buyer.name,
            'gstin': b.buyer.gstin or '-',
            'weight': b.total_weight,
            'bastani': b.total_amount,
            'packing': b.packing,
            'hamali': b.hamali,
            'wm_fee': b.weighman_fee,
            'commission': b.commission,
            'cess': b.cess,
            'taxable_amount': taxable_amount,
            'sgst': (b.gst or 0) / 2,
            'cgst': (b.gst or 0) / 2,
            'round_off': b.round_off,
            'grand_total': b.grand_total,
        })
    
    context = {
        'from_date': from_date_obj,
        'to_date': to_date_obj,
        'report_data': report_data,
        'totals': {
            'weight': sum(r['weight'] or 0 for r in report_data),
            'bastani': sum(r['bastani'] or 0 for r in report_data),
            'packing': sum(r['packing'] or 0 for r in report_data),
            'hamali': sum(r['hamali'] or 0 for r in report_data),
            'wm_fee': sum(r['wm_fee'] or 0 for r in report_data),
            'commission': sum(r['commission'] or 0 for r in report_data),
            'cess': sum(r['cess'] or 0 for r in report_data),
            'taxable_amount': sum(r['taxable_amount'] or 0 for r in report_data),
            'sgst': sum(r['sgst'] or 0 for r in report_data),
            'cgst': sum(r['cgst'] or 0 for r in report_data),
            'round_off': sum(r['round_off'] or 0 for r in report_data),
            'grand_total': sum(r['grand_total'] or 0 for r in report_data),
        },
        'system_name': "MSBC-2025-26",
        'auto_print': request.GET.get('print') == '1',
    }
    return render(request, "accounts/reports/detailed_gst.html", context)


@login_required
def cess_report(request):
    from_date = request.GET.get('from_date')
    to_date = request.GET.get('to_date')
    
    if not (from_date and to_date):
        return redirect('gst_reports')
    
    from_date_obj = date.fromisoformat(from_date)
    to_date_obj = date.fromisoformat(to_date)
    
    # Image 3 shows Cess Report listing bills
    bills = TraderBill.objects.filter(
        date__range=[from_date, to_date]
    ).select_related('buyer').order_by('date', 'invoice_no')
    
    report_data = []
    for b in bills:
        # Resolve 'Name and Place'
        name_place = f"{b.buyer.name}"
        if b.buyer.address:
            name_place += f" & {b.buyer.address}"
            
        report_data.append({
            'date': b.date,
            'bill_no': b.invoice_no,
            'name_place': name_place,
            'weight': b.total_weight,
            'bags': b.total_bags,
            'amount': b.total_amount,
            'cess': b.cess,
            'w_fee': b.weighman_fee,
        })
    
    context = {
        'from_date': from_date_obj,
        'to_date': to_date_obj,
        'report_data': report_data,
        'totals': {
            'weight': sum(r['weight'] or 0 for r in report_data),
            'bags': sum(r['bags'] or 0 for r in report_data),
            'amount': sum(r['amount'] or 0 for r in report_data),
            'cess': sum(r['cess'] or 0 for r in report_data),
            'w_fee': sum(r['w_fee'] or 0 for r in report_data),
        },
        'system_name': "MSBC-2025-26",
        'auto_print': request.GET.get('print') == '1',
    }
    return render(request, "accounts/reports/cess_report.html", context)


@login_required
def weekly_cess_report(request):
    from django.db.models.functions import TruncWeek
    from django.db.models import Sum
    
    from_date = request.GET.get('from_date')
    to_date = request.GET.get('to_date')
    
    if not (from_date and to_date):
        return redirect('gst_reports')
    
    from_date_obj = date.fromisoformat(from_date)
    to_date_obj = date.fromisoformat(to_date)
    
    weeks = TraderBill.objects.filter(
        date__range=[from_date, to_date]
    ).annotate(
        week=TruncWeek('date')
    ).values('week').annotate(
        weight=Sum('total_weight'),
        bags=Sum('total_bags'),
        amount=Sum('total_amount'),
        cess=Sum('cess'),
        w_fee=Sum('weighman_fee'),
    ).order_by('week')
    
    report_data = []
    for w in weeks:
        report_data.append({
            'week_display': f"Week of {w['week'].strftime('%d-%m-%Y')}",
            'weight': w['weight'],
            'bags': w['bags'],
            'amount': w['amount'],
            'cess': w['cess'],
            'w_fee': w['w_fee'],
        })
    
    context = {
        'from_date': from_date_obj,
        'to_date': to_date_obj,
        'report_data': report_data,
        'totals': {
            'weight': sum(r['weight'] or 0 for r in report_data),
            'bags': sum(r['bags'] or 0 for r in report_data),
            'amount': sum(r['amount'] or 0 for r in report_data),
            'cess': sum(r['cess'] or 0 for r in report_data),
            'w_fee': sum(r['w_fee'] or 0 for r in report_data),
        },
        'system_name': "MSBC-2025-26",
        'auto_print': request.GET.get('print') == '1',
    }
    return render(request, "accounts/reports/weekly_cess.html", context)


@login_required
def gstr1_report(request):
    from_date = request.GET.get('from_date')
    to_date = request.GET.get('to_date')
    
    if not (from_date and to_date):
        return redirect('gst_reports')
    
    from_date_obj = date.fromisoformat(from_date)
    to_date_obj = date.fromisoformat(to_date)
    
    bills = TraderBill.objects.filter(
        date__range=[from_date, to_date]
    ).select_related('buyer').order_by('date', 'invoice_no')
    
    b2b_data = []
    b2c_data = []
    
    for b in bills:
        taxable_amount = (b.total_amount or 0) + (b.packing or 0) + (b.hamali or 0) + \
                        (b.weighman_fee or 0) + (b.commission or 0) + (b.cess or 0)
        
        row = {
            'date': b.date,
            'bill_no': b.invoice_no,
            'name': b.buyer.name,
            'gstin': b.buyer.gstin or '',
            'taxable_amount': taxable_amount,
            'igst': 0, # Assuming intra-state for simplicity
            'cgst': (b.gst or 0) / 2,
            'sgst': (b.gst or 0) / 2,
            'total_gst': b.gst,
            'grand_total': b.grand_total,
        }
        
        if b.buyer.gstin:
            b2b_data.append(row)
        else:
            b2c_data.append(row)
            
    context = {
        'from_date': from_date_obj,
        'to_date': to_date_obj,
        'b2b_data': b2b_data,
        'b2c_data': b2c_data,
        'system_name': "MSBC-2025-26",
        'auto_print': request.GET.get('print') == '1',
    }
    return render(request, "accounts/reports/gstr1_report.html", context)


@login_required
def partywise_gst_report(request):
    from django.db.models import Sum
    
    from_date = request.GET.get('from_date')
    to_date = request.GET.get('to_date')
    
    if not (from_date and to_date):
        return redirect('gst_reports')
    
    from_date_obj = date.fromisoformat(from_date)
    to_date_obj = date.fromisoformat(to_date)
    
    parties = TraderBill.objects.filter(
        date__range=[from_date, to_date]
    ).values('buyer__name', 'buyer__gstin').annotate(
        weight=Sum('total_weight'),
        bastani=Sum('total_amount'),
        packing=Sum('packing'),
        hamali=Sum('hamali'),
        wm_fee=Sum('weighman_fee'),
        commission=Sum('commission'),
        cess=Sum('cess'),
        gst_total=Sum('gst'),
        round_off=Sum('round_off'),
        grand_total=Sum('grand_total'),
    ).order_by('buyer__name')
    
    report_data = []
    for p in parties:
        taxable_amount = (p['bastani'] or 0) + (p['packing'] or 0) + (p['hamali'] or 0) + \
                        (p['wm_fee'] or 0) + (p['commission'] or 0) + (p['cess'] or 0)
        report_data.append({
            'name': p['buyer__name'],
            'gstin': p['buyer__gstin'] or '-',
            'weight': p['weight'],
            'bastani': p['bastani'],
            'packing': p['packing'],
            'hamali': p['hamali'],
            'wm_fee': p['wm_fee'],
            'commission': p['commission'],
            'cess': p['cess'],
            'taxable_amount': taxable_amount,
            'sgst': (p['gst_total'] or 0) / 2,
            'cgst': (p['gst_total'] or 0) / 2,
            'round_off': p['round_off'],
            'grand_total': p['grand_total'],
        })
        
    context = {
        'from_date': from_date_obj,
        'to_date': to_date_obj,
        'report_data': report_data,
        'totals': {
            'weight': sum(r['weight'] or 0 for r in report_data),
            'taxable_amount': sum(r['taxable_amount'] or 0 for r in report_data),
            'grand_total': sum(r['grand_total'] or 0 for r in report_data),
        },
        'system_name': "MSBC-2025-26",
        'auto_print': request.GET.get('print') == '1',
    }
    return render(request, "accounts/reports/partywise_gst.html", context)


@login_required
def bazar_kharidi(request):
    from datetime import date
    from django.db.models import Sum
    
    selected_date_str = request.GET.get("date")
    if selected_date_str:
        try:
            selected_date = date.fromisoformat(selected_date_str)
        except ValueError:
            selected_date = date.today()
    else:
        selected_date = date.today()
        
    mode = "bill"
    
    traders = Trader.objects.all().order_by("name")
    
    report_data = []
    total_as_on = 0.0
    total_all = 0.0
    
    for trader in traders:
        opening = float(trader.opening_balance or 0)
        if trader.balance_type == "credit":
            opening = -opening
            
        # Only show traders who had active transactions (Bikri, BagTransfer, or TraderBill) on this specific selected date
        has_bill = TraderBill.objects.filter(buyer=trader, date=selected_date).exists()
        has_bikri = Bikri.objects.filter(buyer=trader, date=selected_date, is_cancelled=False).exists()
        from accounts.models import BagTransfer
        has_transfer = BagTransfer.objects.filter(target_buyer=trader, date=selected_date).exists()
        
        if not (has_bill or has_bikri or has_transfer):
            continue
            
        # As On Date: Day-specific total
        bill_day = TraderBill.objects.filter(buyer=trader, date=selected_date).first()
        if bill_day:
            balance_as_on = float(bill_day.grand_total)
        else:
            bikris_day_sum = Bikri.objects.filter(
                buyer=trader, date=selected_date, is_cancelled=False
            ).aggregate(total=Sum("total_amount"))["total"] or 0.0
            transfers_day_sum = BagTransfer.objects.filter(
                target_buyer=trader, date=selected_date
            ).aggregate(total=Sum("amount"))["total"] or 0.0
            balance_as_on = float(bikris_day_sum) + float(transfers_day_sum)
        
        # Account Balance: Cumulative balance up to the selected date
        bills_all = TraderBill.objects.filter(
            buyer=trader, date__lte=selected_date
        ).aggregate(total=Sum("grand_total"))["total"] or 0.0
        balance_all = opening + float(bills_all)
        
        report_data.append({
            "name": trader.name,
            "short_code": trader.short_code or trader.name,
            "balance_as_on": balance_as_on,
            "balance_all": balance_all,
        })
        total_as_on += balance_as_on
        total_all += balance_all
        
    context = {
        "selected_date": selected_date.strftime("%Y-%m-%d"),
        "selected_date_display": selected_date.strftime("%d-%m-%Y"),
        "mode": mode,
        "report_data": report_data,
        "total_as_on": total_as_on,
        "total_all": total_all,
        "system_name": "MSBC-2025-26",
        "auto_print": request.GET.get("print") == "1",
    }
    return render(request, "accounts/bazar_kharidi.html", context)


@login_required
def nondha(request):
    from datetime import date, timedelta
    from django.db.models import Sum
    
    from_date_str = request.GET.get("from_date")
    to_date_str = request.GET.get("to_date")
    
    if from_date_str:
        try:
            from_date = date.fromisoformat(from_date_str)
        except ValueError:
            from_date = date(2025, 4, 1)
    else:
        from_date = date(2025, 4, 1)
        
    if to_date_str:
        try:
            to_date = date.fromisoformat(to_date_str)
        except ValueError:
            to_date = date.today()
    else:
        to_date = date.today()
        
    dates = Bikri.objects.filter(
        date__range=[from_date, to_date], is_cancelled=False
    ).values_list("date", flat=True).distinct().order_by("date")
    
    report_data = []
    total_credit = 0.0
    total_debit = 0.0
    total_diff = 0.0
    
    for d in dates:
        debit = 0.0
        credit = 0.0
        
        buyers = Bikri.objects.filter(date=d, is_cancelled=False).values_list("buyer_id", flat=True).distinct()
        for buyer_id in buyers:
            bill = TraderBill.objects.filter(date=d, buyer_id=buyer_id).first()
            if bill:
                credit += float(bill.grand_total)
                debit += float(bill.grand_total)
            else:
                bikris_sum = Bikri.objects.filter(
                    date=d, buyer_id=buyer_id, is_cancelled=False
                ).aggregate(total=Sum("total_amount"))["total"] or 0.0
                debit += float(bikris_sum)
                
        if credit == 0 and debit == 0:
            continue
            
        diff = abs(debit - credit)
        
        report_data.append({
            "date": d,
            "credit": credit,
            "debit": debit,
            "difference": diff,
        })
        total_credit += credit
        total_debit += debit
        total_diff += diff
        
    context = {
        "from_date": from_date.strftime("%Y-%m-%d"),
        "to_date": to_date.strftime("%Y-%m-%d"),
        "from_date_display": from_date.strftime("%d-%m-%Y"),
        "to_date_display": to_date.strftime("%d-%m-%Y"),
        "report_data": report_data,
        "total_credit": total_credit,
        "total_debit": total_debit,
        "total_diff": total_diff,
        "system_name": "MSBC-2025-26",
        "auto_print": request.GET.get("print") == "1",
    }
    return render(request, "accounts/nondha.html", context)


@login_required
def pategalu(request):
    date_param = request.GET.get("date")
    if date_param:
        try:
            selected = date.fromisoformat(date_param)
        except ValueError:
            selected = date.today()
    else:
        selected = date.today()

    selected_str = selected.strftime("%Y-%m-%d")

    # Trader Bills: saved ಖರೀದಿ ಪಟ್ಟಿ for this date
    trader_bills = (
        TraderBill.objects.filter(date=selected)
        .select_related("buyer")
        .order_by("invoice_no")
    )
    trader_total = sum(float(b.grand_total) for b in trader_bills)

    # Farmer Bills: merge by bill no + farmer, show combined net payable
    bikri_entries = (
        Bikri.objects.filter(date=selected, is_cancelled=False)
        .select_related("avak__farmer")
        .order_by("bill_no", "avak__lot_number")
    )
    from collections import OrderedDict

    farmer_groups = OrderedDict()
    for b in bikri_entries:
        bill_no = (b.bill_no or "").strip()
        farmer_id = b.avak.farmer_id
        group_key = (bill_no, farmer_id) if bill_no else ("", farmer_id)
        if group_key not in farmer_groups:
            farmer_groups[group_key] = {
                "bill_no": bill_no,
                "farmer_name": b.avak.farmer.name,
                "bikris": [],
            }
        farmer_groups[group_key]["bikris"].append(b)

    farmer_bills = []
    farmer_total = 0
    for group in farmer_groups.values():
        net = float(_calc_group_net_payable(group["bikris"], selected))
        farmer_bills.append(
            {
                "bill_no": group["bill_no"],
                "farmer_name": group["farmer_name"],
                "amount": net,
            }
        )
        farmer_total += net

    def _bill_sort_key(row):
        bill_no = row["bill_no"]
        if bill_no.isdigit():
            return (0, int(bill_no), row["farmer_name"])
        return (1, bill_no, row["farmer_name"])

    farmer_bills.sort(key=_bill_sort_key)

    return render(
        request,
        "accounts/pategalu.html",
        {
            "selected_date": selected_str,
            "selected_date_display": selected.strftime("%d-%m-%Y"),
            "trader_bills": trader_bills,
            "trader_total": trader_total,
            "farmer_bills": farmer_bills,
            "farmer_total": farmer_total,
        },
    )


@login_required
def delivery_book(request):
    from django.db.models import Sum

    date_param = request.GET.get("date")
    if date_param:
        try:
            selected = date.fromisoformat(date_param)
        except ValueError:
            selected = date.today()
    else:
        selected = date.today()

    selected_str = selected.strftime("%Y-%m-%d")
    selected_display = selected.strftime("%d-%m-%Y")

    from accounts.models import BagTransfer

    # Normal Bikri rows
    bikri_qs = (
        Bikri.objects.filter(date=selected, is_cancelled=False)
        .values("buyer__id", "buyer__short_code", "buyer__name")
        .annotate(total_bags=Sum("no_of_bags"))
    )

    trader_map = {}
    for row in bikri_qs:
        tid = row["buyer__id"]
        trader_map[tid] = {
            "id": tid,
            "short_code": row["buyer__short_code"] or row["buyer__name"],
            "avak_bags": row["total_bags"] or 0,
            "bikri_bags": row["total_bags"] or 0,
            "balance": 0,
        }

    # Add transferred bags (target_buyer gets credit)
    transfers = BagTransfer.objects.filter(date=selected)
    for t in transfers:
        tid = t.target_buyer.id
        if tid not in trader_map:
            trader_map[tid] = {
                "id": tid,
                "short_code": t.target_buyer.short_code or t.target_buyer.name,
                "avak_bags": 0,
                "bikri_bags": 0,
                "balance": 0,
            }
        trader_map[tid]["avak_bags"] += t.no_of_bags
        trader_map[tid]["bikri_bags"] += t.no_of_bags

    trader_data = sorted(trader_map.values(), key=lambda x: x["short_code"] or "")
    total_avak = sum(r["avak_bags"] for r in trader_data)
    total_bikri = sum(r["bikri_bags"] for r in trader_data)

    return render(
        request,
        "accounts/delivery_book.html",
        {
            "selected_date": selected_str,
            "selected_date_display": selected_display,
            "trader_data": trader_data,
            "total_avak_bags": total_avak,
            "total_bikri_bags": total_bikri,
            "total_balance": total_avak - total_bikri,
            "auto_print": request.GET.get("print") == "1",
        },
    )



@login_required
def akada(request):
    from django.db.models.functions import Length as DbLength

    date_param = request.GET.get("date")
    if date_param:
        try:
            selected = date.fromisoformat(date_param)
        except ValueError:
            selected = date.today()
    else:
        selected = date.today()

    selected_str = selected.strftime("%Y-%m-%d")
    selected_trader_id = request.GET.get("trader_id", "")

    traders_all = (
        Trader.objects.exclude(short_code__isnull=True)
        .exclude(short_code="")
        .order_by("short_code")
    )

    # Build per-trader lot-wise data for this date
    bikris = (
        Bikri.objects.filter(date=selected, is_cancelled=False)
        .select_related("buyer", "avak")
        .prefetch_related("weights")
        .order_by("buyer__short_code", DbLength("avak__lot_number"), "avak__lot_number")
    )

    from accounts.models import BagTransfer

    # Group by trader
    trader_dict = {}
    for b in bikris:
        tid = b.buyer_id
        if tid not in trader_dict:
            trader_dict[tid] = {
                "id": tid,
                "short_code": b.buyer.short_code or b.buyer.name,
                "lots": [],
            }
        trader_dict[tid]["lots"].append(
            {
                "lot_no": b.avak.lot_number if b.avak else "-",
                "bags": b.no_of_bags,
                "weight": float(b.total_weight),
                "rate": float(b.rate),
                "amount": float(b.amount),
                "bag_weights": [float(w.weight) for w in b.weights.all().order_by("bag_no")],
            }
        )

    # Add transferred bags
    transfers = BagTransfer.objects.filter(date=selected).select_related('target_buyer').prefetch_related('weights')
    for t in transfers:
        tid = t.target_buyer.id
        if tid not in trader_dict:
            trader_dict[tid] = {
                "id": tid,
                "short_code": t.target_buyer.short_code or t.target_buyer.name,
                "lots": [],
            }
        trader_dict[tid]["lots"].append(
            {
                "lot_no": "-",
                "bags": t.no_of_bags,
                "weight": float(t.total_weight),
                "rate": float(t.rate),
                "amount": float(t.amount),
                "bag_weights": [float(w.weight) for w in t.weights.all().order_by("bag_no")],
                "transfer_id": t.id,
                "is_transfer": True,
            }
        )

    akada_list = list(trader_dict.values())

    return render(
        request,
        "accounts/akada.html",
        {
            "selected_date": selected_str,
            "selected_trader_id": selected_trader_id,
            "traders": traders_all,
            "akada_json": json.dumps(akada_list),
        },
    )


def _build_bikri_grouped(selected_date):
    """Return a list of GroupedBikri objects for the given date, sorted by lot number."""
    from django.db.models.functions import Length
    from collections import OrderedDict

    bikris_qs = (
        Bikri.objects.filter(is_cancelled=False, date=selected_date)
        .select_related("avak", "buyer", "avak__farmer")
        .order_by(Length("avak__lot_number"), "avak__lot_number")
    )

    grouped_map = OrderedDict()
    for b in bikris_qs:
        farmer = b.avak.farmer
        buyer = b.buyer
        bill_no = (b.bill_no or "").strip()
        key = farmer.id  # merge all lots for same farmer on the date
        if key not in grouped_map:
            grouped_map[key] = {
                "id": b.id,
                "date": b.date,
                "farmer": farmer,
                "bill_no": bill_no,
                "buyer_names": [],
                "place": b.avak.place or "",
                "lots": [],
                "no_of_bags": 0,
                "total_weight": Decimal("0.000"),
                "bikris": [],
                "lot_details": [],
            }
        grouped_map[key]["bikris"].append(b)
        if buyer.name not in grouped_map[key]["buyer_names"]:
            grouped_map[key]["buyer_names"].append(buyer.name)
        if b.avak.lot_number not in grouped_map[key]["lots"]:
            grouped_map[key]["lots"].append(b.avak.lot_number)
            grouped_map[key]["lot_details"].append({"id": b.id, "lot_number": b.avak.lot_number})
        grouped_map[key]["no_of_bags"] += b.no_of_bags
        grouped_map[key]["total_weight"] += b.total_weight

    class GroupedBikri:
        def __init__(self, bikri_id, bdate, farmer, buyer_name, lot_number, no_of_bags, total_weight, net_payable, place="", lot_details=None, bill_no=""):
            self.id = bikri_id
            self.date = bdate
            self.no_of_bags = no_of_bags
            self.total_weight = total_weight
            self.net_payable = net_payable
            self.total_amount = net_payable  # backward compat for templates expecting total_amount
            self.place = place
            self.lot_details = lot_details or []
            self.bill_no = bill_no

            class MockBuyer:
                def __init__(self, name):
                    self.name = name
            self.buyer = MockBuyer(buyer_name)

            class MockAvak:
                def __init__(self, lot_no, farm):
                    self.lot_number = lot_no
                    self.farmer = farm
            self.avak = MockAvak(lot_number, farmer)

    def lot_sort_key(lot):
        if lot.isdigit():
            return (0, int(lot), lot)
        return (1, 0, lot)

    result = []
    for data in grouped_map.values():
        sorted_lots = sorted(data["lots"], key=lot_sort_key)
        lot_number_str = ", ".join(sorted_lots)
        sorted_lot_details = []
        for lot_no in sorted_lots:
            for detail in data["lot_details"]:
                if detail["lot_number"] == lot_no:
                    sorted_lot_details.append(detail)
                    break
        result.append(GroupedBikri(
            bikri_id=data["id"],
            bdate=data["date"],
            farmer=data["farmer"],
            buyer_name=", ".join(data["buyer_names"]),
            lot_number=lot_number_str,
            no_of_bags=data["no_of_bags"],
            total_weight=data["total_weight"],
            net_payable=_calc_group_net_payable(data["bikris"], selected_date),
            place=data["place"],
            lot_details=sorted_lot_details,
            bill_no=data["bill_no"],
        ))
    return result


@login_required
def bikri_list(request):
    from datetime import date

    date_param = request.GET.get("date")
    if date_param:
        try:
            selected_date = date.fromisoformat(date_param)
        except ValueError:
            selected_date = date.today()
    else:
        selected_date = date.today()

    bikris_grouped = _build_bikri_grouped(selected_date)

    return render(
        request,
        "accounts/bikri_list.html",
        {
            "bikris": bikris_grouped,
            "date_value": selected_date.strftime("%Y-%m-%d"),
        },
    )


@login_required
@login_required
def get_next_bill_no(request):
    from django.http import JsonResponse
    from .models import Bikri
    exclude_id = request.GET.get("exclude_id")  # for edit: exclude current bikri
    check_no = request.GET.get("check")          # kept for backward compatibility
    entry_date = request.GET.get("date")         # for day-scoped bill number

    if check_no:
        # Same bill number can contain multiple lots for the same customer/day.
        return JsonResponse({"duplicate": False})

    # Day-scoped: next bill no is numeric max bill_no + 1 for that date.
    # This avoids gaps/skips when multiple lots share one bill number.
    if entry_date:
        date_qs = Bikri.objects.filter(date=entry_date, is_cancelled=False)
        if exclude_id:
            date_qs = date_qs.exclude(id=exclude_id)

        max_bill_no = 0
        for raw_bill_no in date_qs.exclude(bill_no="").exclude(bill_no__isnull=True).values_list("bill_no", flat=True):
            try:
                bill_no_int = int(str(raw_bill_no).strip())
                if bill_no_int > max_bill_no:
                    max_bill_no = bill_no_int
            except (TypeError, ValueError):
                continue

        next_no = max_bill_no + 1 if max_bill_no > 0 else 1
    else:
        # Fallback: global max + 1 (old behaviour)
        last = Bikri.objects.exclude(bill_no="").exclude(bill_no__isnull=True).order_by("-id").first()
        next_no = 1
        if last and last.bill_no:
            try:
                next_no = int(last.bill_no) + 1
            except (ValueError, TypeError):
                next_no = Bikri.objects.exclude(bill_no="").exclude(bill_no__isnull=True).count() + 1

    return JsonResponse({"next_bill_no": next_no})


def add_bikri(request):

    if request.method == "POST":
        entry_date = request.POST.get("date")
        bill_no = request.POST.get("bill_no", "").strip()
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        lot_id = request.POST.get("lot_id")
        buyer_id = request.POST.get("buyer_id")
        no_of_bags = _to_int(request.POST.get("no_of_bags"))
        rate = _to_decimal(request.POST.get("rate"))
        total_weight = _to_decimal(request.POST.get("total_weight"))
        amount = _to_decimal(request.POST.get("amount"))
        hamali = _to_decimal(request.POST.get("hamali"))
        packing = _to_decimal(request.POST.get("packing"))
        dalali = _to_decimal(request.POST.get("dalali"))
        cess = _to_decimal(request.POST.get("cess"))
        weighman_fee = _to_decimal(request.POST.get("weighman_fee"))
        gst = _to_decimal(request.POST.get("gst"))
        total_amount = _to_decimal(request.POST.get("total_amount"))
        farmer_hamali = _to_decimal(request.POST.get("farmer_hamali"))
        farmer_packing = _to_decimal(request.POST.get("farmer_packing"))
        rent = _to_decimal(request.POST.get("rent"))
        unload_fee = _to_decimal(request.POST.get("unload_fee"))
        other_fee_1 = _to_decimal(request.POST.get("other_fee_1"))
        other_fee_2 = _to_decimal(request.POST.get("other_fee_2"))

        # Rakham (%) deduction is controlled from Settings (Market Rates)
        try:
            entry_date_obj = date.fromisoformat(entry_date) if entry_date else None
        except ValueError:
            entry_date_obj = None

        rakham_percent = _get_rakham_percent_for_date(entry_date_obj)
        cash_deduct = (amount * rakham_percent) / Decimal("100")
        cash_deduct = cash_deduct.quantize(Decimal("0.01"))

        hamali_rate = _to_decimal(request.POST.get("hamali_rate"))
        packing_rate = _to_decimal(request.POST.get("packing_rate"))
        dalali_rate = _to_decimal(request.POST.get("dalali_rate"))
        cess_rate = _to_decimal(request.POST.get("cess_rate"))
        gst_rate = _to_decimal(request.POST.get("gst_rate"))
        farmer_hamali_rate = _to_decimal(request.POST.get("farmer_hamali_rate"))
        farmer_packing_rate = _to_decimal(request.POST.get("farmer_packing_rate"))
        farmer_unloading_rate = _to_decimal(request.POST.get("farmer_unloading_rate"))

        # Individual weights JSON
        weights_data = request.POST.get("weights_json")  # List of {bag_no, weight}

        try:
            avak = Avak.objects.get(id=lot_id)
            _validate_bikri_bag_entry(avak, entry_date, no_of_bags, weights_data)

            bill_amount = amount - farmer_hamali + farmer_packing
            net_payable = (
                bill_amount - cash_deduct - rent - unload_fee - other_fee_1 - other_fee_2
            )
            net_payable = net_payable.quantize(Decimal("0.01"))

            bikri = Bikri.objects.create(
                date=entry_date,
                avak_id=lot_id,
                buyer_id=buyer_id,
                no_of_bags=no_of_bags,
                rate=rate,
                total_weight=total_weight,
                amount=amount,
                hamali=hamali,
                packing=packing,
                dalali=dalali,
                cess=cess,
                weighman_fee=weighman_fee,
                gst=gst,
                total_amount=total_amount,
                farmer_hamali=farmer_hamali,
                farmer_packing=farmer_packing,
                rent=rent,
                unload_fee=unload_fee,
                cash_deduct=cash_deduct,
                other_fee_1=other_fee_1,
                other_fee_2=other_fee_2,
                net_payable=net_payable,
                hamali_rate=hamali_rate,
                packing_rate=packing_rate,
                dalali_rate=dalali_rate,
                cess_rate=cess_rate,
                gst_rate=gst_rate,
                farmer_hamali_rate=farmer_hamali_rate,
                farmer_packing_rate=farmer_packing_rate,
                farmer_unloading_rate=farmer_unloading_rate,
                bill_no=request.POST.get("bill_no", "").strip(),
            )

            if weights_data:
                weights = json.loads(weights_data)
                for w in weights:
                    BikriBagWeight.objects.create(
                        bikri=bikri, bag_no=w["bag_no"], weight=w["weight"]
                    )

            # Auto-generate (or regenerate) ledger voucher so account statement is populated.
            # Delete any existing auto-voucher for this farmer+date group first so that
            # adding a second/third lot always regenerates the voucher with ALL current lots.
            try:
                _group_qs = Bikri.objects.filter(
                    date=bikri.date,
                    avak__farmer=bikri.avak.farmer,
                    is_cancelled=False,
                )
                Voucher.objects.filter(ref_bikri__in=_group_qs, is_auto=True).delete()
                _build_bikri_voucher(bikri)
            except Exception:
                pass  # Never block bikri save due to ledger errors

            messages.success(request, "Bikri entry added successfully.")

            next_lot_id = request.POST.get("next_lot_id")

            if is_ajax:
                return JsonResponse({
                    'success': True,
                    'bikri_id': bikri.id,
                    'bill_no': str(bikri.bill_no) if bikri.bill_no else '',
                    'lot_no': bikri.avak.lot_number,
                    'farmer_name': bikri.avak.farmer.name,
                    'no_of_bags': bikri.no_of_bags,
                    'total_weight': str(bikri.total_weight),
                    'rate': str(bikri.rate),
                    'amount': str(bikri.amount),
                    'farmer_hamali': str(bikri.farmer_hamali),
                    'farmer_packing': str(bikri.farmer_packing),
                    'net_payable': str(bikri.net_payable),
                    'next_lot_id': next_lot_id or '',
                    'entry_date': entry_date,
                    'buyer_id': str(buyer_id) if buyer_id else '',
                    'buyer_rate': str(rate),
                })

            if next_lot_id:
                return redirect(
                    f"{reverse('add_bikri')}?lot_no={next_lot_id}&date={entry_date}&rate={rate}&buyer_id={buyer_id}"
                )

            return redirect(f"{reverse('add_bikri')}?date={entry_date}")
        except Exception as e:
            if is_ajax:
                return JsonResponse({'success': False, 'error': str(e)})
            messages.error(request, f"Error saving Bikri: {str(e)}")

    today = date.today()
    prefill_lot = request.GET.get("lot_no")
    date_val = request.GET.get("date") or today.strftime("%Y-%m-%d")
    prefill_rate = request.GET.get("rate")
    prefill_buyer_id = request.GET.get("buyer_id")
    prefill_buyer = None
    if prefill_buyer_id:
        prefill_buyer = Trader.objects.filter(id=prefill_buyer_id).first()

    try:
        selected_date = date.fromisoformat(date_val)
    except ValueError:
        selected_date = today
    bikris_grouped = _build_bikri_grouped(selected_date)

    return render(
        request,
        "accounts/add_bikri.html",
        {
            "date_value": date_val,
            "prefill_lot": prefill_lot,
            "prefill_rate": prefill_rate,
            "prefill_buyer": prefill_buyer,
            "bikris": bikris_grouped,
        },
    )


@login_required
def edit_bikri_multi(request, bikri_id):
    """Edit No Of Bags, Weight and Rate for all lots of the same farmer on the same date."""
    primary = get_object_or_404(Bikri, id=bikri_id)
    all_lots = (
        Bikri.objects.filter(
            date=primary.date,
            avak__farmer=primary.avak.farmer,
            is_cancelled=False,
        )
        .select_related("avak", "buyer", "avak__farmer")
        .order_by("avak__lot_number")
    )

    if request.method == "POST":
        errors = []
        rates_obj = (
            MarketRate.objects.filter(date__lte=primary.date)
            .order_by("-date")
            .first()
        )
        weighman_rate = rates_obj.weighman_fee_per_bag if rates_obj else Decimal("0")
        rakham_percent = _get_rakham_percent_for_date(primary.date)

        with transaction.atomic():
            for bikri in all_lots:
                bags_key = f"no_of_bags_{bikri.id}"
                weight_key = f"total_weight_{bikri.id}"
                rate_key = f"rate_{bikri.id}"

                no_of_bags = _to_int(request.POST.get(bags_key, bikri.no_of_bags))
                total_weight = _to_decimal(request.POST.get(weight_key, bikri.total_weight))
                rate = _to_decimal(request.POST.get(rate_key, bikri.rate))

                amount = (total_weight / Decimal("100")) * rate
                amount = amount.quantize(Decimal("0.01"))

                dalali = (amount * bikri.dalali_rate / Decimal("100")).quantize(Decimal("0.01"))
                cess = (amount * bikri.cess_rate / Decimal("100")).quantize(Decimal("0.01"))
                gst = (amount * bikri.gst_rate / Decimal("100")).quantize(Decimal("0.01"))
                hamali = (no_of_bags * bikri.hamali_rate).quantize(Decimal("0.01"))
                packing = (no_of_bags * bikri.packing_rate).quantize(Decimal("0.01"))
                weighman_fee = (no_of_bags * weighman_rate).quantize(Decimal("0.01"))
                total_amount = amount + dalali + cess + gst + hamali + packing + weighman_fee

                farmer_hamali = (no_of_bags * bikri.farmer_hamali_rate).quantize(Decimal("0.01"))
                farmer_packing = (no_of_bags * bikri.farmer_packing_rate).quantize(Decimal("0.01"))
                unload_fee = (no_of_bags * bikri.farmer_unloading_rate).quantize(Decimal("0.01"))
                cash_deduct = (amount * rakham_percent / Decimal("100")).quantize(Decimal("0.01"))

                bill_amount = amount - farmer_hamali + farmer_packing
                net_payable = (
                    bill_amount - cash_deduct - bikri.rent - unload_fee - bikri.other_fee_1 - bikri.other_fee_2
                ).quantize(Decimal("0.01"))

                bikri.no_of_bags = no_of_bags
                bikri.total_weight = total_weight
                bikri.rate = rate
                bikri.amount = amount
                bikri.dalali = dalali
                bikri.cess = cess
                bikri.gst = gst
                bikri.hamali = hamali
                bikri.packing = packing
                bikri.weighman_fee = weighman_fee
                bikri.total_amount = total_amount
                bikri.farmer_hamali = farmer_hamali
                bikri.farmer_packing = farmer_packing
                bikri.unload_fee = unload_fee
                bikri.cash_deduct = cash_deduct
                bikri.net_payable = net_payable
                bikri.save()

        # Regenerate voucher so account statement reflects updated rates/weights
        try:
            _group_qs = Bikri.objects.filter(
                date=primary.date,
                avak__farmer=primary.avak.farmer,
                is_cancelled=False,
            )
            Voucher.objects.filter(ref_bikri__in=_group_qs, is_auto=True).delete()
            _build_bikri_voucher(primary)
        except Exception:
            pass  # Never block bikri save due to ledger errors

        if errors:
            messages.error(request, "; ".join(errors))
        else:
            messages.success(request, "All lots updated successfully.")
        return redirect("bikri_list")

    return render(
        request,
        "accounts/edit_bikri_multi.html",
        {
            "primary": primary,
            "all_lots": all_lots,
            "farmer": primary.avak.farmer,
            "date_value": primary.date.strftime("%Y-%m-%d"),
        },
    )


@login_required
def edit_bikri(request, bikri_id):
    bikri = Bikri.objects.get(id=bikri_id)
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if request.method == "POST":
        entry_date = request.POST.get("date")
        bill_no = request.POST.get("bill_no", "").strip()
        lot_id = request.POST.get("lot_id")
        buyer_id = request.POST.get("buyer_id")
        no_of_bags = _to_int(request.POST.get("no_of_bags"))
        rate = _to_decimal(request.POST.get("rate"))
        total_weight = _to_decimal(request.POST.get("total_weight"))
        amount = _to_decimal(request.POST.get("amount"))
        hamali = _to_decimal(request.POST.get("hamali"))
        packing = _to_decimal(request.POST.get("packing"))
        dalali = _to_decimal(request.POST.get("dalali"))
        cess = _to_decimal(request.POST.get("cess"))
        weighman_fee = _to_decimal(request.POST.get("weighman_fee"))
        gst = _to_decimal(request.POST.get("gst"))
        total_amount = _to_decimal(request.POST.get("total_amount"))
        farmer_hamali = _to_decimal(request.POST.get("farmer_hamali"))
        farmer_packing = _to_decimal(request.POST.get("farmer_packing"))
        rent = _to_decimal(request.POST.get("rent"))
        unload_fee = _to_decimal(request.POST.get("unload_fee"))
        other_fee_1 = _to_decimal(request.POST.get("other_fee_1"))
        other_fee_2 = _to_decimal(request.POST.get("other_fee_2"))

        # Rakham (%) deduction is controlled from Settings (Market Rates)
        try:
            entry_date_obj = date.fromisoformat(entry_date) if entry_date else None
        except ValueError:
            entry_date_obj = None

        rakham_percent = _get_rakham_percent_for_date(entry_date_obj)
        cash_deduct = (amount * rakham_percent) / Decimal("100")
        cash_deduct = cash_deduct.quantize(Decimal("0.01"))

        bill_amount = amount - farmer_hamali + farmer_packing
        net_payable = (
            bill_amount - cash_deduct - rent - unload_fee - other_fee_1 - other_fee_2
        )
        net_payable = net_payable.quantize(Decimal("0.01"))

        hamali_rate = _to_decimal(request.POST.get("hamali_rate"))
        packing_rate = _to_decimal(request.POST.get("packing_rate"))
        dalali_rate = _to_decimal(request.POST.get("dalali_rate"))
        cess_rate = _to_decimal(request.POST.get("cess_rate"))
        gst_rate = _to_decimal(request.POST.get("gst_rate"))
        farmer_hamali_rate = _to_decimal(request.POST.get("farmer_hamali_rate"))
        farmer_packing_rate = _to_decimal(request.POST.get("farmer_packing_rate"))
        farmer_unloading_rate = _to_decimal(request.POST.get("farmer_unloading_rate"))

        weights_data = request.POST.get("weights_json")

        try:
            avak = Avak.objects.get(id=lot_id)
            _validate_bikri_bag_entry(
                avak, entry_date, no_of_bags, weights_data, exclude_bikri_id=bikri.id
            )

            bikri.date = entry_date
            bikri.avak_id = lot_id
            bikri.buyer_id = buyer_id
            bikri.no_of_bags = no_of_bags
            bikri.rate = rate
            bikri.total_weight = total_weight
            bikri.amount = amount
            bikri.hamali = hamali
            bikri.packing = packing
            bikri.dalali = dalali
            bikri.cess = cess
            bikri.weighman_fee = weighman_fee
            bikri.gst = gst
            bikri.total_amount = total_amount
            bikri.farmer_hamali = farmer_hamali
            bikri.farmer_packing = farmer_packing
            bikri.rent = rent
            bikri.unload_fee = unload_fee
            bikri.cash_deduct = cash_deduct
            bikri.other_fee_1 = other_fee_1
            bikri.other_fee_2 = other_fee_2
            bikri.net_payable = net_payable

            bikri.hamali_rate = hamali_rate
            bikri.packing_rate = packing_rate
            bikri.dalali_rate = dalali_rate
            bikri.cess_rate = cess_rate
            bikri.gst_rate = gst_rate
            bikri.farmer_hamali_rate = farmer_hamali_rate
            bikri.farmer_packing_rate = farmer_packing_rate
            bikri.farmer_unloading_rate = farmer_unloading_rate
            bikri.bill_no = bill_no

            bikri.save()

            if weights_data:
                # Delete old weights and add new ones
                bikri.weights.all().delete()
                weights = json.loads(weights_data)
                for w in weights:
                    BikriBagWeight.objects.create(
                        bikri=bikri, bag_no=w["bag_no"], weight=w["weight"]
                    )

            # Regenerate voucher so account statement reflects updated rate/weight
            try:
                _group_qs = Bikri.objects.filter(
                    date=bikri.date,
                    avak__farmer=bikri.avak.farmer,
                    is_cancelled=False,
                )
                Voucher.objects.filter(ref_bikri__in=_group_qs, is_auto=True).delete()
                _build_bikri_voucher(bikri)
            except Exception:
                pass  # Never block bikri save due to ledger errors

            messages.success(request, "Bikri entry updated successfully.")
            if is_ajax:
                return JsonResponse(
                    {
                        "success": True,
                        "bikri_id": bikri.id,
                        "bill_no": str(bikri.bill_no or ""),
                        "lot_no": bikri.avak.lot_number if bikri.avak else "",
                    }
                )
            return redirect("bikri_list")
        except Exception as e:
            if is_ajax:
                return JsonResponse({"success": False, "error": str(e)})
            messages.error(request, f"Error updating Bikri: {str(e)}")

    # Prepare weights for frontend
    weights_json = json.dumps(
        [{"bag_no": w.bag_no, "weight": float(w.weight)} for w in bikri.weights.all()]
    )
    avak_total_bags = int(bikri.avak.no_of_bags or 0) if bikri.avak else int(bikri.no_of_bags or 0)
    lot_bikris = (
        Bikri.objects.filter(
            avak=bikri.avak, date=bikri.date, is_cancelled=False
        )
        .select_related("buyer")
        .prefetch_related("weights")
        .order_by("id")
    )
    lot_segments = [_bikri_edit_segment_payload(b) for b in lot_bikris]

    return render(
        request,
        "accounts/add_bikri.html",
        {
            "bikri": bikri,
            "weights_json": weights_json,
            "date_value": bikri.date.strftime("%Y-%m-%d") if bikri.date else "",
            "is_edit": True,
            "avak_total_bags": avak_total_bags,
            "lot_segments_json": json.dumps(lot_segments),
        },
    )


@login_required
def view_bikri(request, bikri_id):
    from django.db.models import Min
    from django.db.models.functions import Length

    bikri = Bikri.objects.get(id=bikri_id)

    # Always merge same customer (farmer) lots for the selected date.
    all_lots_qs = (
        Bikri.objects.filter(
            date=bikri.date,
            avak__farmer=bikri.avak.farmer,
            is_cancelled=False,
        )
        .select_related("avak", "buyer", "avak__farmer")
        .order_by(Length("avak__lot_number"), "avak__lot_number", "id")
    )
    all_lots = list(all_lots_qs)
    grouped_lots = _group_bikris_by_lot_for_bill(all_lots)
    lot_numbers_display = ", ".join(g["lot_number"] for g in grouped_lots if g["lot_number"])

    bill_no = (bikri.bill_no or "").strip()
    if bill_no:
        patti_no = bill_no
    else:
        # Fallback sequential patti number for legacy records without bill_no.
        farmer_order = (
            Bikri.objects.filter(date=bikri.date, is_cancelled=False)
            .values("avak__farmer_id")
            .annotate(first_id=Min("id"))
            .order_by("first_id")
        )
        current_farmer_id = bikri.avak.farmer_id
        patti_no = next(
            (i + 1 for i, f in enumerate(farmer_order) if f["avak__farmer_id"] == current_farmer_id),
            1,
        )

    # Grouping is forced now as requested: "Generate single bill per farmer per date"
    # Even if there's only one lot, we treat it as a collection of 1.
    
    # Totals
    total_bags = sum(b.no_of_bags for b in all_lots)
    total_weight = sum(b.total_weight for b in all_lots)
    total_amount = sum(b.amount for b in all_lots)  # Original ರಕಮು
    
    # Fetch rates for that date
    market_rates = MarketRate.objects.filter(date=bikri.date).first()
    if market_rates:
        farmer_hamali_rate = market_rates.farmer_hamali_per_bag
        farmer_packing_rate = market_rates.farmer_packing_per_bag
        rakham_percent = market_rates.rakham_percent
    else:
        farmer_hamali_rate = bikri.farmer_hamali_rate or 0
        farmer_packing_rate = bikri.farmer_packing_rate or 0
        rakham_percent = _get_rakham_percent_for_date(bikri.date)
    
    # Deductions aggregated (Auto-recalculated based on rates)
    farmer_hamali = total_bags * farmer_hamali_rate
    farmer_packing = total_bags * farmer_packing_rate
    
    # Pull deductions once per avak lot (supports split sales to multiple buyers)
    rent, unload_fee, other_fee_1, other_fee_2 = _sum_avak_deductions_once(all_lots)
    
    # Rakham amount on combined total amount
    rakham_amount = _calculate_rakham_amount(total_amount, rakham_percent)
    
    bill_amount = total_amount - farmer_hamali + farmer_packing
    other_deductions_total = rent + unload_fee + other_fee_1 + other_fee_2
    
    net_payable_calc = bill_amount - rakham_amount - other_deductions_total
    net_payable_calc = _quantize_money(net_payable_calc)
    
    # Resolve display name and village (Kannada preferred, overrides if set)
    farmer = bikri.avak.farmer if bikri.avak and bikri.avak.farmer else None
    avak_place = bikri.avak.place if bikri.avak else None
    display_name = _farmer_display_name(farmer, bikri.farmer_name_override)
    display_village = _farmer_display_place(farmer, avak_place, bikri.village_override)
    
    context = {
        "is_combined": True, # Always show the merged table layout
        "bikri": bikri,
        "display_name": display_name,
        "display_village": display_village,
        "all_lots": all_lots,
        "grouped_lots": grouped_lots,
        "lot_numbers_display": lot_numbers_display,
        "lot_count": len(grouped_lots),
        "total_bags": total_bags,
        "total_weight": total_weight,
        "total_amount": total_amount,
        "farmer_hamali": farmer_hamali,
        "farmer_packing": farmer_packing,
        "farmer_hamali_rate": farmer_hamali_rate,
        "farmer_packing_rate": farmer_packing_rate,
        "amount_after_farmer_hamali": total_amount - farmer_hamali,
        "bill_amount": bill_amount,
        "rakham_percent": rakham_percent,
        "rakham_amount": rakham_amount,
        "other_deductions_total": other_deductions_total,
        "net_payable_calc": net_payable_calc,
        "rent": rent,
        "unload_fee": unload_fee,
        "other_fee_1": other_fee_1,
        "other_fee_2": other_fee_2,
        "patti_no": patti_no,
        "auto_print": request.GET.get("print") == "1",
    }
    return render(request, "accounts/view_bikri.html", context)


@login_required
def edit_cancel_dashboard(request):
    today = date.today().strftime("%Y-%m-%d")
    return render(request, "accounts/edit_cancel.html", {"today": today})


@login_required
def get_traders_by_date(request):
    d = request.GET.get("date")
    if not d:
        return JsonResponse({"results": []})

    t_ids_avak = Avak.objects.filter(
        date=d, buyer__isnull=False, is_cancelled=False
    ).values_list("buyer_id", flat=True)
    t_ids_bikri = Bikri.objects.filter(
        date=d, is_cancelled=False
    ).values_list("buyer_id", flat=True)
    t_ids_bill = TraderBill.objects.filter(date=d).values_list("buyer_id", flat=True)

    all_t_ids = set(list(t_ids_avak) + list(t_ids_bikri) + list(t_ids_bill))
    traders = Trader.objects.filter(id__in=all_t_ids).order_by("name")

    results = [
        {"id": t.id, "text": f"{t.short_code or t.name} ({t.name})"}
        for t in traders
    ]
    return JsonResponse({"results": results})


@login_required
def get_lots_by_date(request):
    d = request.GET.get("date")
    if not d:
        return JsonResponse({"results": []})
    
    avaks = Avak.objects.filter(date=d, is_cancelled=False).select_related("buyer").order_by("lot_number")
    results = []

    bikri_avak_ids = set(
        Bikri.objects.filter(avak__date=d, is_cancelled=False).values_list("avak_id", flat=True)
    )
    bikri_buyers = {
        b.avak_id: b.buyer
        for b in Bikri.objects.filter(avak__date=d, is_cancelled=False).select_related("buyer")
    }

    for a in avaks:
        avak_rate, avak_buyer_code, avak_buyer_id = _avak_tender_fields(a)
        buyer = a.buyer or bikri_buyers.get(a.id)
        buyer_name = avak_buyer_code or (
            f"{buyer.short_code or buyer.name}" if buyer else "-"
        )
        results.append({
            "id": a.id,
            "lot_number": a.lot_number,
            "buyer_id": avak_buyer_id or (buyer.id if buyer else ""),
            "buyer_name": buyer_name,
            "has_bikri": a.id in bikri_avak_ids,
            "rate": avak_rate,
            "no_of_bags": a.no_of_bags,
        })
    return JsonResponse({"results": results})


@login_required
def get_bills_by_date(request):
    d = request.GET.get("date")
    if not d:
        return JsonResponse({"results": []})
    
    bikris = Bikri.objects.filter(date=d, is_cancelled=False).select_related("avak__farmer")
    results = []
    for b in bikris:
        farmer_name = b.farmer_name_override or (b.avak.farmer.name if b.avak and b.avak.farmer else "-")
        village = b.village_override or (b.avak.place if b.avak else "-")
        results.append({
            "id": b.id,
            "lot_number": b.avak.lot_number if b.avak else "-",
            "farmer_name": farmer_name,
            "village": village
        })
    return JsonResponse({"results": results})


@login_required
def transfer_all_lots(request):
    if request.method == "POST":
        d = request.POST.get("date")
        old_id = request.POST.get("old_trader_id")
        new_id = request.POST.get("new_trader_id")
        
        # Update Avak
        Avak.objects.filter(date=d, buyer_id=old_id, is_cancelled=False).update(buyer_id=new_id)
        # Update Bikri
        Bikri.objects.filter(date=d, buyer_id=old_id, is_cancelled=False).update(buyer_id=new_id)
        
        # Recalculate bills for both traders
        _recalculate_and_save_trader_bill(d, old_id)
        _recalculate_and_save_trader_bill(d, new_id)

        return JsonResponse({"success": True, "message": "All lots transferred successfully."})
    return JsonResponse({"success": False, "message": "Invalid request."})


def _market_rates_for_date(bill_date):
    """Latest market rates on or before bill_date."""
    return MarketRate.objects.filter(date__lte=bill_date).order_by("-date").first()


def _apply_trader_market_charges(bill, rates=None):
    """Set trader-side charges on a TraderBill from market rates (per bag / %)."""
    if rates is None:
        rates = _market_rates_for_date(bill.date)
    if not rates:
        return bill

    total_bags = bill.total_bags or 0
    total_amount = _to_decimal(bill.total_amount)

    bill.commission = _quantize_money((total_amount * rates.dalali_percent) / Decimal("100"))
    bill.packing = _quantize_money(total_bags * rates.trader_packing_per_bag)
    bill.hamali = _quantize_money(total_bags * rates.trader_hamali_per_bag)
    bill.weighman_fee = _quantize_money(total_bags * rates.weighman_fee_per_bag)
    bill.cess = _quantize_money((total_amount * rates.cess_percent) / Decimal("100"))

    taxable_amount = (
        total_amount + bill.commission + bill.packing + bill.hamali
        + bill.weighman_fee + bill.cess
    )
    bill.gst = _quantize_money((taxable_amount * rates.gst_percent) / Decimal("100"))
    grand_total = taxable_amount + bill.gst
    rounded_grand_total = grand_total.quantize(Decimal("0"))
    bill.round_off = rounded_grand_total - grand_total
    bill.grand_total = rounded_grand_total
    return bill


def _recalculate_and_save_trader_bill(bill_date, trader_id):
    """
    Recalculates a trader's bill for a specific date based on all non-cancelled
    Bikri entries and saves it. This is crucial for ensuring data consistency
    after edits or cancellations.
    """
    from django.db import transaction

    bikris = Bikri.objects.filter(
        date=bill_date, buyer_id=trader_id, is_cancelled=False
    )

    # If no bikris are left, delete the bill if it exists
    if not bikris.exists():
        TraderBill.objects.filter(date=bill_date, buyer_id=trader_id).delete()
        return

    rates = _market_rates_for_date(bill_date)
    if not rates:
        return

    total_bags = sum(b.no_of_bags for b in bikris)
    total_weight = sum(b.total_weight for b in bikris)
    total_amount = sum(b.amount for b in bikris)  # Bastani

    with transaction.atomic():
        import uuid
        temp_invoice_no = f"temp_{uuid.uuid4().hex[:10]}"
        bill, created = TraderBill.objects.get_or_create(
            date=bill_date, buyer_id=trader_id,
            defaults={'invoice_no': temp_invoice_no}
        )

        bill.total_bags = total_bags
        bill.total_weight = total_weight
        bill.total_amount = total_amount
        _apply_trader_market_charges(bill, rates)

        if created:
            # Generate a real invoice number by finding the highest numeric invoice number excluding the current bill
            max_inv = 0
            digit_bills = TraderBill.objects.exclude(id=bill.id).filter(invoice_no__regex=r"^\d+$")
            for db in digit_bills:
                if db.invoice_no.isdigit():
                    max_inv = max(max_inv, int(db.invoice_no))
            bill.invoice_no = str(max_inv + 1)

        bill.save()

        # Re-link Bikri entries
        TraderBillItem.objects.filter(bill=bill).delete()
        for b in bikris:
            TraderBillItem.objects.create(bill=bill, bikri=b)


@login_required
def transfer_lot_wise(request):
    if request.method == "POST":
        lot_id = request.POST.get("lot_id")
        new_id = request.POST.get("new_trader_id")
        
        avak = Avak.objects.get(id=lot_id)
        old_trader_id = avak.buyer_id
        bill_date = avak.date

        avak.buyer_id = new_id
        avak.save()
        
        # Also update corresponding Bikri entries if they exist
        Bikri.objects.filter(avak=avak, is_cancelled=False).update(buyer_id=new_id)
        
        # Recalculate bills
        if old_trader_id:
            _recalculate_and_save_trader_bill(bill_date, old_trader_id)
        _recalculate_and_save_trader_bill(bill_date, new_id)

        return JsonResponse({"success": True, "message": "Lot transferred successfully."})
    return JsonResponse({"success": False, "message": "Invalid request."})


@login_required
def update_farmer_details(request):
    if request.method == "POST":
        bill_id = request.POST.get("bill_id")
        name = request.POST.get("farmer_name")
        village = request.POST.get("farmer_village")
        
        bikri = Bikri.objects.get(id=bill_id)
        # Update ALL entries for this farmer on this date to ensure the merged bill reflects it
        Bikri.objects.filter(
            date=bikri.date, 
            avak__farmer=bikri.avak.farmer, 
            is_cancelled=False
        ).update(farmer_name_override=name, village_override=village)
            
        return JsonResponse({"success": True, "message": "Farmer details updated ONLY for this bill."})
    return JsonResponse({"success": False, "message": "Invalid request."})


@login_required
def cancel_vikri_patti(request):
    if request.method == "POST":
        d = request.POST.get("date")
        
        # Find all affected traders before cancelling
        affected_traders = list(Bikri.objects.filter(date=d, is_cancelled=False).values_list('buyer_id', flat=True).distinct())

        Bikri.objects.filter(date=d, is_cancelled=False).update(is_cancelled=True)

        # Recalculate for all affected traders
        for trader_id in affected_traders:
            if trader_id:
                _recalculate_and_save_trader_bill(d, trader_id)

        return JsonResponse({"success": True, "message": "Vikri Patti cancelled for this date."})
    return JsonResponse({"success": False, "message": "Invalid request."})


@login_required
def cancel_kharidi_patti(request):
    if request.method == "POST":
        d = request.POST.get("date")

        # Find all affected traders before cancelling
        affected_traders = list(Bikri.objects.filter(date=d, is_cancelled=False).values_list('buyer_id', flat=True).distinct())

        Avak.objects.filter(date=d, is_cancelled=False).update(is_cancelled=True)
        Bikri.objects.filter(date=d, is_cancelled=False).update(is_cancelled=True)

        # Recalculate for all affected traders
        for trader_id in affected_traders:
            if trader_id:
                _recalculate_and_save_trader_bill(d, trader_id)

        return JsonResponse({"success": True, "message": "Kharidi Patti cancelled for this date."})
    return JsonResponse({"success": False, "message": "Invalid request."})


@login_required
def delete_bikri(request, bikri_id):
    try:
        bikri = Bikri.objects.get(id=bikri_id)
        bill_date = bikri.date
        trader_id = bikri.buyer_id
        farmer = bikri.avak.farmer
        bikri.is_cancelled = True
        bikri.save()
        
        # Soft delete the linked Avak entry
        if bikri.avak:
            bikri.avak.is_cancelled = True
            bikri.avak.save()

        # Update account statement: delete old voucher; rebuild if remaining lots exist
        try:
            _group_qs = Bikri.objects.filter(
                date=bill_date,
                avak__farmer=farmer,
                is_cancelled=False,
            )
            Voucher.objects.filter(ref_bikri__in=_group_qs, is_auto=True).delete()
            # Also delete voucher linked to the cancelled bikri itself
            Voucher.objects.filter(ref_bikri=bikri, is_auto=True).delete()
            if _group_qs.exists():
                _build_bikri_voucher(_group_qs.first())
        except Exception:
            pass  # Never block deletion due to ledger errors

        if bill_date and trader_id:
            _recalculate_and_save_trader_bill(bill_date, trader_id)
        messages.success(request, "Bikri entry and linked lot deleted successfully.")
    except Exception as e:
        messages.error(request, f"Error deleting entry: {str(e)}")
    
    date_str = request.GET.get("date") or (bikri.date.isoformat() if 'bikri' in locals() else "")
    url = reverse("bikri_list")
    if date_str:
        url += f"?date={date_str}"
    return redirect(url)


@login_required
def get_lot_details(request):
    lot_no = request.GET.get("lot_no")
    entry_date = request.GET.get("date")
    buyer_id = (request.GET.get("buyer_id") or "").strip()
    exclude_bikri_id = (request.GET.get("exclude_bikri_id") or "").strip()
    exclude_id = int(exclude_bikri_id) if exclude_bikri_id.isdigit() else None

    qs = Avak.objects.filter(lot_number__iexact=lot_no, is_cancelled=False)
    if entry_date:
        qs = qs.filter(date=entry_date)

    avak = qs.select_related("buyer", "farmer").first()
    if avak:
        # Get all lots for this farmer on this date (arrival date)
        # Exclude the current lot to show ONLY "other" lots in the hint
        all_lots_qs = Avak.objects.filter(
            farmer=avak.farmer, date=avak.date, is_cancelled=False
        ).exclude(id=avak.id)

        sold_avak_ids = Bikri.objects.filter(
            avak__farmer=avak.farmer,
            avak__date=avak.date,
            is_cancelled=False,
        )
        if exclude_id:
            sold_avak_ids = sold_avak_ids.exclude(id=exclude_id)
        sold_avak_ids = sold_avak_ids.values_list("avak_id", flat=True)

        other_lots = all_lots_qs.exclude(id__in=sold_avak_ids).values_list(
            "lot_number", flat=True
        )

        coverage = _get_avak_bikri_coverage(
            avak, entry_date or avak.date, exclude_bikri_id=exclude_id
        )
        already_sold = coverage["fully_sold"]

        # Suggest existing bill number and preload already saved lots
        # for this same farmer/date (any buyer — combined bill).
        existing_bikri_qs = Bikri.objects.filter(
            avak__farmer=avak.farmer,
            date=avak.date,
            is_cancelled=False,
        ).select_related("avak", "buyer")

        if buyer_id.isdigit():
            buyer_bill_qs = existing_bikri_qs.filter(buyer_id=int(buyer_id))
        else:
            buyer_bill_qs = existing_bikri_qs

        latest_with_bill = (
            existing_bikri_qs.exclude(bill_no="")
            .exclude(bill_no__isnull=True)
            .order_by("-id")
            .first()
        )
        if buyer_id.isdigit() and not latest_with_bill:
            latest_with_bill = (
                buyer_bill_qs.exclude(bill_no="")
                .exclude(bill_no__isnull=True)
                .order_by("-id")
                .first()
            )

        suggested_bill_no = ""
        previous_bill_lots = []
        if latest_with_bill and latest_with_bill.bill_no:
            suggested_bill_no = str(latest_with_bill.bill_no).strip()
            bill_lots_qs = (
                existing_bikri_qs.filter(bill_no=suggested_bill_no)
                .select_related("avak", "buyer")
                .order_by("id")
            )
            previous_bill_lots = [
                {
                    "lot_no": b.avak.lot_number if b.avak else "",
                    "buyer_name": (b.buyer.short_code or b.buyer.name) if b.buyer else "",
                    "bags": b.no_of_bags,
                    "weight": float(b.total_weight or 0),
                    "rate": float(b.rate or 0),
                    "amount": float(b.amount or 0),
                    "farmer_hamali": float(b.farmer_hamali or 0),
                    "farmer_packing": float(b.farmer_packing or 0),
                }
                for b in bill_lots_qs
            ]

        farmer = avak.farmer
        avak_rate, avak_buyer_code, avak_buyer_id = _avak_tender_fields(avak)
        data = {
            "id": avak.id,
            "farmer_name": _farmer_display_name(farmer),
            "farmer_id": avak.farmer.id,
            "place": _farmer_display_place(farmer, avak.place),
            "no_of_bags": avak.no_of_bags,
            "already_sold": already_sold,
            "remaining_bags": coverage["remaining"],
            "saved_segments": coverage["segments"],
            "used_bag_nos": sorted(coverage["used_bag_nos"]),
            "hamali_rate": str(avak.hamali_rate),
            "freight": str(avak.freight),
            "hamali_total": str(avak.hamali_total),
            "advance": str(avak.advance),
            "empty_bags": avak.empty_bags,
            "other_lots": list(other_lots),
            "suggested_bill_no": suggested_bill_no,
            "previous_bill_lots": previous_bill_lots,
            "tender_rate": avak_rate,
            "buyer_id": avak_buyer_id or "",
            "buyer_code": avak_buyer_code,
            "buyer_name": avak.buyer.name if avak.buyer else "",
        }
        return JsonResponse({"success": True, "data": data})
    return JsonResponse({"success": False})


@login_required
def check_lot_number(request):
    lot_number = (request.GET.get("lot_number") or "").strip()
    entry_date = (request.GET.get("date") or "").strip()
    exclude_id = (request.GET.get("exclude_id") or "").strip()

    qs = Avak.objects.filter(is_cancelled=False)
    if exclude_id.isdigit():
        qs = qs.exclude(id=int(exclude_id))

    exists = False
    if lot_number and entry_date:
        exists = qs.filter(lot_number__iexact=lot_number, date=entry_date).exists()

    return JsonResponse({"exists": exists})


def _reset_sqlite_sequences(models_to_reset):
    table_names = [model._meta.db_table for model in models_to_reset]
    if not table_names:
        return

    with connection.cursor() as cursor:
        for table_name in table_names:
            try:
                cursor.execute(
                    "DELETE FROM sqlite_sequence WHERE name = %s;",
                    [table_name],
                )
            except Exception:
                pass


def _seed_admin():
    if not User.objects.filter(username="admin@admin.com").exists():
        User.objects.create_superuser("admin@admin.com", "admin@admin.com", "123")


def login_view(request):
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            auth_login(request, user)
            return redirect("home")
        messages.error(request, "Invalid username or password")
    return render(request, "accounts/login.html")


@login_required
def home(request):
    today = date.today()
    total_avak_week = 0
    total_vikri_week = 0
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        total_avak_week += Avak.objects.filter(date=d, is_cancelled=False).count()
        total_vikri_week += Bikri.objects.filter(date=d, is_cancelled=False).count()

    new_accounts = []
    for f in Farmer.objects.order_by("-created_at")[:10]:
        new_accounts.append({
            "type_kn": "ರೈತ",
            "type_en": "Farmer",
            "name": f.name,
            "name_kn": f.name_kannada or "—",
            "phone": f.phone or "—",
            "created_at": f.created_at,
            "edit_url": reverse("edit_farmer", args=[f.id]),
        })
    for t in Trader.objects.order_by("-created_at")[:10]:
        new_accounts.append({
            "type_kn": "ವ್ಯಾಪಾರಿ",
            "type_en": "Trader",
            "name": t.name,
            "name_kn": t.name_kannada or "—",
            "phone": t.phone or "—",
            "created_at": t.created_at,
            "edit_url": reverse("edit_trader", args=[t.id]),
        })
    new_accounts.sort(key=lambda x: x["created_at"], reverse=True)
    new_accounts = new_accounts[:10]

    context = {
        "user": request.user,
        "today": today,
        "total_avak_today": Avak.objects.filter(date=today, is_cancelled=False).count(),
        "total_vikri_today": Bikri.objects.filter(date=today, is_cancelled=False).count(),
        "total_kharidi_today": TraderBill.objects.filter(date=today).count(),
        "total_farmer_bill_today": Bikri.objects.filter(
            date=today, is_cancelled=False
        ).values("avak__farmer").distinct().count(),
        "total_avak_week": total_avak_week,
        "total_vikri_week": total_vikri_week,
        "new_accounts": new_accounts,
        "total_farmers": Farmer.objects.count(),
        "total_traders": Trader.objects.count(),
        "total_accounts": LedgerAccount.objects.count(),
    }
    return render(request, "accounts/dashboard.html", context)


@login_required
def truncate_records(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Permission denied.")

    if request.method != "POST":
        messages.error(request, "Invalid request method.")
        return redirect("home")

    delete_all = request.POST.get("delete_all") == "1"

    transaction_models = [
        VoucherLine,
        Voucher,
        FinancialTransaction,
        TraderBillItem,
        TraderBill,
        BagTransferWeight,
        BagTransfer,
        BikriBagWeight,
        Bikri,
        Avak,
    ]

    master_models = [
        MarketRate,
        Trader,
        Farmer,
        Users,
        User,
    ]

    models_to_delete = transaction_models + master_models if delete_all else transaction_models

    with transaction.atomic():
        for model in models_to_delete:
            model.objects.all().delete()
        _reset_sqlite_sequences(models_to_delete)

    ensure_default_ledgers()

    if delete_all:
        _seed_admin()
        messages.success(
            request,
            "All transaction and party records deleted. "
            "Ledger Master (groups & accounts) preserved. "
            "Admin recreated as admin@admin.com / 123.",
        )
    else:
        messages.success(request, "Transaction records deleted. Master data preserved.")

    return redirect("home")


def logout_view(request):
    auth_logout(request)
    return redirect("login")


@login_required
def user_list(request):
    users = Users.objects.all()
    q = request.GET.get("q")
    if q:
        users = users.filter(
            Q(full_name__icontains=q) | Q(email__icontains=q) | Q(contact__icontains=q)
        )
    return render(request, "accounts/user_list.html", {"users": users})


@login_required
def add_user(request):
    if request.method == "POST":
        full_name = request.POST.get("full_name")
        contact = request.POST.get("contact")
        email = request.POST.get("email")
        password = request.POST.get("password")
        confirm_password = request.POST.get("confirm_password")
        user_type = request.POST.get("type")

        if password != confirm_password:
            messages.error(request, "Passwords do not match!")
            return redirect("add_user")

        if contact and Users.objects.filter(contact=contact).exists():
            messages.error(request, "Phone number already exists!")
            return redirect("add_user")

        if not User.objects.filter(username=email).exists():
            User.objects.create_user(
                username=email, email=email, password=password, first_name=full_name
            )

        Users.objects.create(
            full_name=full_name,
            contact=contact,
            email=email,
            password=password,
            type=user_type,
        )
        messages.success(request, "User added successfully.")
        return redirect("user_list")

    return render(request, "accounts/add_user.html")


@login_required
def edit_user(request, user_id):
    user_obj = Users.objects.get(id=user_id)
    if request.method == "POST":
        old_email = user_obj.email
        contact = request.POST.get("contact")

        if (
            contact
            and Users.objects.filter(contact=contact).exclude(id=user_id).exists()
        ):
            messages.error(request, "Phone number already exists!")
            return redirect("edit_user", user_id=user_id)

        user_obj.full_name = request.POST.get("full_name")
        user_obj.contact = contact
        user_obj.email = request.POST.get("email")
        user_obj.type = request.POST.get("type")

        password = request.POST.get("password")
        if password:
            confirm_password = request.POST.get("confirm_password")
            if password == confirm_password:
                user_obj.password = password
            else:
                messages.error(request, "Passwords do not match!")
                return redirect("edit_user", user_id=user_id)

        user_obj.save()

        auth_user = User.objects.filter(username=old_email).first()
        if auth_user:
            auth_user.username = user_obj.email
            auth_user.email = user_obj.email
            auth_user.first_name = user_obj.full_name
            if password:
                auth_user.set_password(password)
            auth_user.save()

        messages.success(request, "User updated successfully.")
        return redirect("user_list")

    return render(request, "accounts/edit_user.html", {"user_obj": user_obj})


@login_required
def delete_user(request, user_id):
    try:
        user_obj = Users.objects.get(id=user_id)
        auth_user = User.objects.filter(username=user_obj.email).first()
        if auth_user:
            auth_user.delete()
        user_obj.delete()
        messages.success(request, "User deleted successfully.")
    except Exception as e:
        messages.error(request, f"Error deleting user: {str(e)}")
    return redirect("user_list")


@login_required
def manage_account(request):
    from django.contrib.auth import update_session_auth_hash

    user_obj = Users.objects.filter(email=request.user.username).first()
    if request.method == "POST":
        full_name = request.POST.get("full_name")
        contact = request.POST.get("contact")
        email = request.POST.get("email")
        password = request.POST.get("password")
        confirm_password = request.POST.get("confirm_password")

        if password and password != confirm_password:
            messages.error(request, "Passwords do not match!")
            return redirect("manage_account")

        # Update Users model
        if user_obj:
            user_obj.full_name = full_name
            user_obj.contact = contact
            user_obj.email = email
            if password:
                user_obj.password = password
            user_obj.save()

        # Update auth User model
        auth_user = request.user
        auth_user.username = email
        auth_user.email = email
        auth_user.first_name = full_name
        if password:
            auth_user.set_password(password)
        auth_user.save()

        # Keep user logged in after password change
        if password:
            update_session_auth_hash(request, auth_user)

        messages.success(request, "Account updated successfully.")
        return redirect("manage_account")

    return render(request, "accounts/manage_account.html", {"user_obj": user_obj})


@login_required
def get_next_lot_number(request):
    entry_date = request.GET.get("date")
    if not entry_date:
        return JsonResponse({"success": False, "message": "Date is required"})

    active_lots = set()
    digit_lots = Avak.objects.filter(date=entry_date, lot_number__regex=r"^\d+$", is_cancelled=False)
    for l in digit_lots:
        if l.lot_number.isdigit():
            active_lots.add(int(l.lot_number))
    next_lot = 1
    while next_lot in active_lots:
        next_lot += 1

    return JsonResponse({"success": True, "next_lot": str(next_lot)})


@login_required
def tender_form(request):
    date_param = request.GET.get("date")
    trader_id = request.GET.get("trader_id")

    if date_param:
        try:
            today = date.fromisoformat(date_param)
        except ValueError:
            today = date.today()
    else:
        today = date.today()

    avaks = Avak.objects.filter(date=today, is_cancelled=False).order_by("lot_number")
    traders = Trader.objects.all()

    selected_trader = None
    if trader_id:
        selected_trader = Trader.objects.filter(id=trader_id).first()

    # Calculate totals
    total_bags = sum(a.no_of_bags for a in avaks)

    return render(
        request,
        "accounts/tender_form.html",
        {
            "avaks": avaks,
            "traders": traders,
            "selected_trader": selected_trader,
            "date_value": today.strftime("%Y-%m-%d"),
            "total_bags": total_bags,
            "no_of_lots": avaks.count(),
        },
    )


@login_required
def market_rates(request):
    # Get the latest market rates or create a default one for today
    selected_date_str = request.GET.get("date", date.today().strftime("%Y-%m-%d"))
    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        selected_date = date.today()
        selected_date_str = selected_date.strftime("%Y-%m-%d")

    try:
        rates = MarketRate.objects.get(date=selected_date)
    except MarketRate.DoesNotExist:
        # If not found for selected date, try getting the most recent one to pre-fill
        latest = MarketRate.objects.order_by("-date").first()
        if latest:
            # We don't save it yet, just use as template for the form
            rates = MarketRate(
                date=selected_date,
                farmer_packing_per_bag=latest.farmer_packing_per_bag,
                farmer_hamali_per_bag=latest.farmer_hamali_per_bag,
                farmer_unloading_per_bag=latest.farmer_unloading_per_bag,
                trader_packing_per_bag=latest.trader_packing_per_bag,
                trader_hamali_per_bag=latest.trader_hamali_per_bag,
                weighman_fee_per_bag=latest.weighman_fee_per_bag,
                dalali_percent=latest.dalali_percent,
                cess_percent=latest.cess_percent,
                gst_percent=latest.gst_percent,
                rakham_percent=latest.rakham_percent,
                tds_percent=latest.tds_percent,
                print_farmer_weights=latest.print_farmer_weights,
                print_detailed_bikri_bill=latest.print_detailed_bikri_bill,
            )
        else:
            rates = MarketRate(date=selected_date)

    if request.method == "POST":
        post_date_str = (request.POST.get("date") or "").strip()
        try:
            post_date = date.fromisoformat(post_date_str)
        except ValueError:
            messages.error(request, "Invalid date.")
            return redirect("market_rates")

        farmer_packing_per_bag = _to_decimal(request.POST.get("farmer_packing_per_bag"))
        farmer_hamali_per_bag = _to_decimal(request.POST.get("farmer_hamali_per_bag"))
        farmer_unloading_per_bag = _to_decimal(
            request.POST.get("farmer_unloading_per_bag")
        )
        trader_packing_per_bag = _to_decimal(request.POST.get("trader_packing_per_bag"))
        trader_hamali_per_bag = _to_decimal(request.POST.get("trader_hamali_per_bag"))
        weighman_fee_per_bag = _to_decimal(request.POST.get("weighman_fee_per_bag"))
        dalali_percent = _to_decimal(request.POST.get("dalali_percent"))
        cess_percent = _to_decimal(request.POST.get("cess_percent"))
        gst_percent = _to_decimal(request.POST.get("gst_percent"))
        rakham_percent = _to_decimal(request.POST.get("rakham_percent"))
        tds_percent = _to_decimal(request.POST.get("tds_percent"))

        if rakham_percent < 0 or rakham_percent > 100:
            messages.error(request, "ರಖಂ (%) must be between 0 and 100.")
            # Rebuild an in-memory object so the user sees what they entered
            rates = MarketRate(
                date=post_date,
                farmer_packing_per_bag=farmer_packing_per_bag,
                farmer_hamali_per_bag=farmer_hamali_per_bag,
                farmer_unloading_per_bag=farmer_unloading_per_bag,
                trader_packing_per_bag=trader_packing_per_bag,
                trader_hamali_per_bag=trader_hamali_per_bag,
                weighman_fee_per_bag=weighman_fee_per_bag,
                dalali_percent=dalali_percent,
                cess_percent=cess_percent,
                gst_percent=gst_percent,
                rakham_percent=rakham_percent,
                tds_percent=tds_percent,
            )

            last_updated_record = MarketRate.objects.order_by("-updated_at").first()
            last_updated_date = (
                last_updated_record.date.strftime("%d-%m-%Y")
                if last_updated_record
                else "N/A"
            )
            return render(
                request,
                "accounts/market_rates.html",
                {
                    "rates": rates,
                    "selected_date": post_date_str,
                    "last_updated_date": last_updated_date,
                },
            )

        if tds_percent < 0 or tds_percent > 100:
            messages.error(request, "TDS (%) must be between 0 and 100.")
            rates = MarketRate(
                date=post_date,
                farmer_packing_per_bag=farmer_packing_per_bag,
                farmer_hamali_per_bag=farmer_hamali_per_bag,
                farmer_unloading_per_bag=farmer_unloading_per_bag,
                trader_packing_per_bag=trader_packing_per_bag,
                trader_hamali_per_bag=trader_hamali_per_bag,
                weighman_fee_per_bag=weighman_fee_per_bag,
                dalali_percent=dalali_percent,
                cess_percent=cess_percent,
                gst_percent=gst_percent,
                rakham_percent=rakham_percent,
                tds_percent=tds_percent,
            )

            last_updated_record = MarketRate.objects.order_by("-updated_at").first()
            last_updated_date = (
                last_updated_record.date.strftime("%d-%m-%Y")
                if last_updated_record
                else "N/A"
            )
            return render(
                request,
                "accounts/market_rates.html",
                {
                    "rates": rates,
                    "selected_date": post_date_str,
                    "last_updated_date": last_updated_date,
                },
            )

        rates, _created = MarketRate.objects.get_or_create(date=post_date)
        rates.farmer_packing_per_bag = farmer_packing_per_bag
        rates.farmer_hamali_per_bag = farmer_hamali_per_bag
        rates.farmer_unloading_per_bag = farmer_unloading_per_bag
        rates.trader_packing_per_bag = trader_packing_per_bag
        rates.trader_hamali_per_bag = trader_hamali_per_bag
        rates.weighman_fee_per_bag = weighman_fee_per_bag
        rates.dalali_percent = dalali_percent
        rates.cess_percent = cess_percent
        rates.gst_percent = gst_percent
        rates.rakham_percent = rakham_percent
        rates.tds_percent = tds_percent
        rates.print_farmer_weights = request.POST.get("print_farmer_weights") == "on"
        rates.print_detailed_bikri_bill = (
            request.POST.get("print_detailed_bikri_bill") == "on"
        )
        rates.save()
        messages.success(
            request, f"Market rates for {post_date_str} saved successfully."
        )
        return redirect(reverse("market_rates") + f"?date={post_date_str}")

    # Get the "Last Updated" date for display
    last_updated_record = MarketRate.objects.order_by("-updated_at").first()
    last_updated_date = (
        last_updated_record.date.strftime("%d-%m-%Y") if last_updated_record else "N/A"
    )

    return render(
        request,
        "accounts/market_rates.html",
        {
            "rates": rates,
            "selected_date": selected_date_str,
            "last_updated_date": last_updated_date,
        },
    )


@login_required
def bank_master(request):
    """Single bank details entry — shown on Kharidi Patti invoice footer."""
    bank = BankMaster.get_settings()
    if request.method == "POST":
        bank.bank_name = request.POST.get("bank_name", "").strip()
        bank.account_holder = request.POST.get("account_holder", "").strip()
        bank.account_number = request.POST.get("account_number", "").strip()
        bank.ifsc_code = request.POST.get("ifsc_code", "").strip()
        bank.branch = request.POST.get("branch", "").strip()
        bank.save()
        messages.success(request, "Bank details saved successfully.")
        return redirect("bank_master")

    return render(request, "accounts/bank_master.html", {"bank": bank})


@login_required
def company_settings(request):
    """Company name, address, GST, phone, logo — used on invoices and reports."""
    company = CompanyProfile.get_settings()
    if request.method == "POST":
        company.company_name = request.POST.get("company_name", "").strip()
        company.company_name_kannada = request.POST.get("company_name_kannada", "").strip()
        company.address = request.POST.get("address", "").strip()
        company.gst_number = request.POST.get("gst_number", "").strip()
        company.phone = request.POST.get("phone", "").strip()
        company.system_label = request.POST.get("system_label", "").strip()
        if request.POST.get("remove_logo") == "1":
            if company.logo:
                company.logo.delete(save=False)
            company.logo = None
        elif request.FILES.get("logo"):
            if company.logo:
                company.logo.delete(save=False)
            company.logo = request.FILES["logo"]
        company.save()
        messages.success(request, "Company profile saved. Invoices and reports will use these details.")
        return redirect("company_settings")

    return render(request, "accounts/company_settings.html", {"company": company})


@login_required
def get_bikri_last_lot(request):
    """Return the maximum lot number (numeric) used in Bikri entries for a given date.
    Used by the frontend to enforce ascending lot order per date."""
    entry_date = request.GET.get("date", "").strip()
    exclude_bikri_id = request.GET.get("exclude_id", "").strip()

    if not entry_date:
        return JsonResponse(
            {"success": False, "max_lot": None, "message": "Date is required"}
        )

    qs = Bikri.objects.filter(date=entry_date, is_cancelled=False).select_related("avak")
    if exclude_bikri_id.isdigit():
        qs = qs.exclude(id=int(exclude_bikri_id))

    exclude_id = int(exclude_bikri_id) if exclude_bikri_id.isdigit() else None
    used_lots = _used_lot_numbers_for_date(entry_date, exclude_id)

    return JsonResponse({
        "success": True,
        "max_lot": max(used_lots) if used_lots else None,
        "used_lots": used_lots,
    })


@login_required
def get_market_rates(request):
    target_date = request.GET.get("date", date.today().strftime("%Y-%m-%d"))
    # Fetch rates for exact date or the closest preceding date
    rates = MarketRate.objects.filter(date__lte=target_date).order_by("-date").first()
    if not rates:
        # Provide some defaults if nothing found
        return JsonResponse(
            {
                "farmer_packing_per_bag": 5.00,
                "farmer_hamali_per_bag": 4.85,
                "farmer_unloading_per_bag": 3.70,
                "trader_packing_per_bag": 5.00,
                "trader_hamali_per_bag": 4.85,
                "weighman_fee_per_bag": 1.75,
                "dalali_percent": 2.00,
                "cess_percent": 0.60,
                "gst_percent": 5.00,
                "rakham_percent": 0.00,
                "tds_percent": 2.00,
            }
        )

    return JsonResponse(
        {
            "farmer_packing_per_bag": float(rates.farmer_packing_per_bag),
            "farmer_hamali_per_bag": float(rates.farmer_hamali_per_bag),
            "farmer_unloading_per_bag": float(rates.farmer_unloading_per_bag),
            "trader_packing_per_bag": float(rates.trader_packing_per_bag),
            "trader_hamali_per_bag": float(rates.trader_hamali_per_bag),
            "weighman_fee_per_bag": float(rates.weighman_fee_per_bag),
            "dalali_percent": float(rates.dalali_percent),
            "cess_percent": float(rates.cess_percent),
            "gst_percent": float(rates.gst_percent),
            "rakham_percent": float(rates.rakham_percent),
            "tds_percent": float(rates.tds_percent),
        }
    )


@login_required
def kharidi_patti_list(request):
    bills = TraderBill.objects.all().select_related("buyer").order_by("-date", "-invoice_no")
    context = {
        "bills": bills,
    }
    return render(request, "accounts/kharidi_patti_list.html", context)


@login_required
def kharidi_patti(request):
    today = date.today().isoformat()
    # Fetch default rates for today if available
    market_rates = MarketRate.objects.filter(date__lte=date.today()).order_by("-date").first()
    context = {
        "today": today,
        "market_rates": market_rates
    }
    return render(request, "accounts/kharidi_patti.html", context)


@login_required
def get_buyer_lots(request):
    d = request.GET.get("date")
    buyer_id = request.GET.get("buyer_id")
    if not (d and buyer_id):
        return JsonResponse({"results": []})

    from django.db.models.functions import Length
    from accounts.models import BagTransfer

    bikris = Bikri.objects.filter(
        date=d,
        buyer_id=buyer_id,
        is_cancelled=False
    ).select_related("avak", "avak__farmer").order_by(Length("avak__lot_number"), "avak__lot_number")

    existing_bill = TraderBill.objects.filter(date=d, buyer_id=buyer_id).first()
    is_billed = existing_bill is not None

    results = []
    for b in bikris:
        billed_item = TraderBillItem.objects.filter(bikri=b).select_related("bill").first()
        results.append({
            "id": b.id,
            "lot_number": b.avak.lot_number,
            "bags": b.no_of_bags,
            "weight": float(b.total_weight),
            "rate": float(b.rate),
            "amount": float(b.amount),
            "farmer": b.avak.farmer.name,
            "hamali": float(b.hamali),
            "packing": float(b.packing),
            "dalali": float(b.dalali),
            "cess": float(b.cess),
            "weighman_fee": float(b.weighman_fee),
            "is_transfer": False,
            "is_billed": billed_item is not None,
            "bill_invoice_no": billed_item.bill.invoice_no if billed_item else None,
        })

    # Include BagTransfer entries for this buyer (no lot number — ownership transfers)
    transfers = BagTransfer.objects.filter(
        date=d,
        target_buyer_id=buyer_id
    ).select_related("source_bikri", "source_bikri__avak")

    for t in transfers:
        results.append({
            "id": f"transfer_{t.id}",   # prefix to distinguish from Bikri IDs
            "lot_number": "",            # NO lot number for transferred bags
            "bags": t.no_of_bags,
            "weight": float(t.total_weight),
            "rate": float(t.rate),
            "amount": float(t.amount),
            "farmer": t.source_bikri.avak.farmer.name,
            "hamali": float(t.hamali),
            "packing": float(t.packing),
            "dalali": float(t.dalali),
            "cess": float(t.cess),
            "weighman_fee": float(t.weighman_fee),
            "is_transfer": True,
        })

    deleted_bikris = Bikri.objects.filter(
        date=d,
        buyer_id=buyer_id,
        is_cancelled=True
    ).select_related("avak", "avak__farmer").order_by(Length("avak__lot_number"), "avak__lot_number")

    deleted_results = []
    for b in deleted_bikris:
        deleted_results.append({
            "id": b.id,
            "lot_number": b.avak.lot_number,
            "bags": b.no_of_bags,
            "weight": float(b.total_weight),
            "rate": float(b.rate),
            "amount": float(b.amount),
            "farmer": b.avak.farmer.name,
            "hamali": float(b.hamali),
            "packing": float(b.packing),
            "dalali": float(b.dalali),
            "cess": float(b.cess),
            "weighman_fee": float(b.weighman_fee),
            "is_transfer": False,
        })

    return JsonResponse({
        "results": results,
        "deleted_results": deleted_results,
        "is_billed": is_billed,
        "invoice_no": existing_bill.invoice_no if existing_bill else None
    })



@login_required
def get_trader_details(request):
    buyer_id = request.GET.get("buyer_id")
    if not buyer_id:
        return JsonResponse({"success": False})
    
    trader = Trader.objects.filter(id=buyer_id).first()
    if not trader:
        return JsonResponse({"success": False})
    
    return JsonResponse({
        "success": True,
        "name": trader.name,
        "short_code": trader.short_code,
        "place": trader.address,
        "gstin": trader.gstin,
        "pan": trader.pan,
        "mobile": trader.mobile_no,
        "email": trader.email,
        "pin": trader.pin,
    })


@login_required
def get_farmer_details(request):
    farmer_id = request.GET.get("farmer_id")
    if not farmer_id:
        return JsonResponse({"success": False})

    farmer = Farmer.objects.filter(id=farmer_id).first()
    if not farmer:
        return JsonResponse({"success": False})

    return JsonResponse(
        {
            "success": True,
            "name": farmer.name,
            "place": farmer.address,
            "phone": farmer.phone,
        }
    )



@login_required
def get_created_bills(request):
    d = request.GET.get("date")
    if not d:
        return JsonResponse({"results": []})
    
    bills = TraderBill.objects.filter(date=d).select_related("buyer").order_by("invoice_no")
    results = []
    for b in bills:
        results.append({
            "id": b.id,
            "invoice_no": b.invoice_no,
            "date": b.date.isoformat(),
            "buyer_id": b.buyer.id,
            "buyer_code": b.buyer.short_code or b.buyer.name[:4].upper(),
            "buyer_name": b.buyer.name,
            "bags": b.total_bags,
            "weight": float(b.total_weight),
            "grand_total": float(b.grand_total)
        })
    return JsonResponse({"results": results})


@login_required
def save_trader_bill(request):
    if request.method == "POST":
        import json
        from django.db import transaction

        try:
            data = json.loads(request.body)

            with transaction.atomic():
                # Check for existing bill first
                bill = TraderBill.objects.filter(date=data['date'], buyer_id=data['buyer_id']).first()

                bikri_ids = [
                    bikri_id for bikri_id in data.get('bikri_ids', [])
                    if not str(bikri_id).startswith("transfer_")
                ]
                for bikri_id in bikri_ids:
                    existing_item = (
                        TraderBillItem.objects.filter(bikri_id=bikri_id)
                        .select_related("bill")
                        .first()
                    )
                    if existing_item and (not bill or existing_item.bill_id != bill.id):
                        return JsonResponse({
                            "success": False,
                            "message": (
                                f"Lot already billed in Buyer Patti Invoice "
                                f"{existing_item.bill.invoice_no}. "
                                f"Delete that bill to regenerate."
                            ),
                        })

                if bill:
                    return JsonResponse({
                        "success": False,
                        "message": (
                            f"Buyer Patti already generated (Invoice {bill.invoice_no}). "
                            f"Only one bill per buyer per day. Delete from history to regenerate."
                        ),
                    })

                # Invoice Number Generation for NEW bill
                last_bill = TraderBill.objects.order_by("-id").first()
                inv_no = 1
                if last_bill:
                    try:
                        inv_no = int(last_bill.invoice_no) + 1
                    except ValueError:
                        inv_no = last_bill.id + 1

                bill = TraderBill(
                    invoice_no=str(inv_no),
                    date=data['date'],
                    buyer_id=data['buyer_id'],
                    total_bags=data['total_bags'],
                    total_weight=data['total_weight'],
                    total_amount=data['total_amount'],
                )
                _apply_trader_market_charges(bill)
                bill.save()

                # Link only actual Bikri IDs (skip transfer_ prefixed IDs)
                for bikri_id in bikri_ids:
                    TraderBillItem.objects.create(bill=bill, bikri_id=bikri_id)

                # ── Create / recreate auto Journal voucher for trader account statement ──
                # Delete existing auto-voucher for this bill (if any) before recreating
                Voucher.objects.filter(ref_trader_bill=bill, is_auto=True).delete()
                _build_trader_bill_voucher(bill)

                return JsonResponse({"success": True, "invoice_no": bill.invoice_no})
        except Exception as e:
            return JsonResponse({"success": False, "message": str(e)})

    return JsonResponse({"success": False, "message": "Invalid request method."})


@login_required
def update_trader_details(request):
    if request.method == "POST":
        import json
        data = json.loads(request.body)
        trader_id = data.get("trader_id")
        trader = Trader.objects.filter(id=trader_id).first()
        if not trader:
            return JsonResponse({"success": False, "message": "Trader not found."})
        
        trader.address = data.get("place", trader.address)
        trader.gstin = data.get("gstin", trader.gstin)
        trader.pan = data.get("pan", trader.pan)
        trader.mobile_no = data.get("mobile", trader.mobile_no)
        trader.email = data.get("email", trader.email)
        trader.pin = data.get("pin", trader.pin)
        # Note: Address field is used for Place/Address context
        trader.save()
        return JsonResponse({"success": True})
    return JsonResponse({"success": False})


@login_required
def delete_trader_bill(request, bill_id):
    """Delete a TraderBill and its linked voucher (called via AJAX POST)."""
    if request.method == "POST":
        bill = get_object_or_404(TraderBill, id=bill_id)
        Voucher.objects.filter(ref_trader_bill=bill, is_auto=True).delete()
        bill.delete()  # cascades to TraderBillItem via FK
        return JsonResponse({"success": True})
    return JsonResponse({"success": False, "message": "Invalid request method."})


@login_required
def view_trader_bill(request, bill_id):
    from accounts.models import BagTransfer
    bill = get_object_or_404(TraderBill, id=bill_id)
    items = TraderBillItem.objects.filter(bill=bill).select_related('bikri', 'bikri__avak')
    
    # Also fetch transferred bags for this buyer on this date
    transfers = BagTransfer.objects.filter(target_buyer=bill.buyer, date=bill.date).select_related('source_bikri', 'source_bikri__avak')

    total_unloading = sum(
        ((item.bikri.avak.hamali_total / item.bikri.avak.no_of_bags * item.bikri.no_of_bags)
         for item in items if hasattr(item, 'bikri') and item.bikri and item.bikri.avak and item.bikri.avak.no_of_bags > 0),
        Decimal('0')
    )
    market_rates = MarketRate.objects.filter(date=bill.date).first()

    # Check if an auto-voucher exists for this trader bill
    auto_voucher = Voucher.objects.filter(ref_trader_bill=bill, is_auto=True).first()
    bank = BankMaster.get_settings()
    bank_has_details = bool(
        bank.bank_name or bank.account_number or bank.ifsc_code or bank.branch
    )

    context = {
        'bill': bill,
        'items': items,
        'transfers': transfers,
        'total_unloading': total_unloading,
        'lot_count': items.count() + transfers.count(),
        'market_rates': market_rates,
        'auto_voucher': auto_voucher,
        'bank': bank,
        'bank_has_details': bank_has_details,
    }
    from accounts.tds_utils import calculate_bill_tds
    context['tds_info'] = calculate_bill_tds(bill)
    return render(request, "accounts/view_trader_bill.html", context)


@login_required
def transfer_trader_lots(request):
    if request.method == "POST":
        import json
        data = json.loads(request.body)
        bikri_ids = data.get("bikri_ids", [])
        to_trader_id = data.get("to_trader_id")
        
        if not (bikri_ids and to_trader_id):
            return JsonResponse({"success": False, "message": "Invalid data."})
            
        Bikri.objects.filter(id__in=bikri_ids).update(buyer_id=to_trader_id)
        return JsonResponse({"success": True})
    return JsonResponse({"success": False})


@login_required
def lot_detail_modification(request):
    today = date.today().isoformat()
    traders = Trader.objects.all().order_by("name")
    context = {
        "today": today,
        "traders": traders,
        "default_avg_bag_weight": float(DEFAULT_AVG_BAG_WEIGHT_KG),
    }
    return render(request, "accounts/lot_detail_modification.html", context)


@login_required
def buyer_dalali_vivara(request):
    """Options page — buyer commission / TDS report (Kharidi Patti)."""
    from accounts.tds_utils import TDS_COMMISSION_THRESHOLD, get_tds_percent_for_date

    today = date.today()
    if today.month >= 4:
        default_from = date(today.year, 4, 1)
    else:
        default_from = date(today.year - 1, 4, 1)

    context = {
        "from_date": request.GET.get("from_date") or default_from.isoformat(),
        "to_date": request.GET.get("to_date") or today.isoformat(),
        "tds_threshold": TDS_COMMISSION_THRESHOLD,
        "tds_percent": get_tds_percent_for_date(today),
    }
    return render(request, "accounts/buyer_dalali_vivara.html", context)


@login_required
def buyer_dalali_vivara_report(request):
    """Buyer commission / TDS report output."""
    from accounts.tds_utils import TDS_COMMISSION_THRESHOLD, build_tds_report_rows

    from_date_str = request.GET.get("from_date")
    to_date_str = request.GET.get("to_date")
    filter_mode = request.GET.get("filter", "all")
    if filter_mode not in ("tds_only", "all"):
        filter_mode = "all"

    if not (from_date_str and to_date_str):
        return redirect("buyer_dalali_vivara")

    from_date_obj = date.fromisoformat(from_date_str)
    to_date_obj = date.fromisoformat(to_date_str)
    bills = TraderBill.objects.filter(
        date__range=[from_date_obj, to_date_obj]
    ).select_related("buyer")
    report_data = build_tds_report_rows(bills, filter_mode=filter_mode)
    totals = {
        "commission": sum(r["commission"] for r in report_data),
        "tds": sum(r["tds"] for r in report_data),
        "count_tds": sum(1 for r in report_data if r["tds_applicable"]),
        "count_no_tds": sum(1 for r in report_data if not r["tds_applicable"]),
    }

    context = {
        "from_date_obj": from_date_obj,
        "to_date_obj": to_date_obj,
        "filter_mode": filter_mode,
        "report_data": report_data,
        "totals": totals,
        "tds_threshold": TDS_COMMISSION_THRESHOLD,
        "auto_print": request.GET.get("print") == "1",
    }
    return render(request, "accounts/buyer_dalali_vivara_report.html", context)


@login_required
def update_avak_tender(request):
    """Save tender rate and buyer on an avak lot (Lot Detail Modification)."""
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST request expected."})

    import json

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "Invalid JSON."})

    entry_date = (data.get("date") or "").strip()
    lot_no = (data.get("lot_no") or "").strip()
    rate = _to_decimal(data.get("rate"), default="0")
    buyer_id = (data.get("buyer_id") or "").strip()

    if not (entry_date and lot_no):
        return JsonResponse({"success": False, "message": "Date and lot number are required."})

    avak = Avak.objects.filter(
        date=entry_date, lot_number__iexact=lot_no, is_cancelled=False
    ).first()
    if not avak:
        return JsonResponse({"success": False, "message": "Avak lot not found."})

    avak.rate = rate
    if buyer_id.isdigit():
        avak.buyer_id = int(buyer_id)
    elif not buyer_id:
        avak.buyer_id = None
    avak.save()

    tender_rate, buyer_code, saved_buyer_id = _avak_tender_fields(avak)
    return JsonResponse({
        "success": True,
        "message": "Tender rate updated.",
        "rate": tender_rate,
        "buyer_code": buyer_code,
        "buyer_id": saved_buyer_id,
    })


def _enrich_pdf_rows_for_preview(rows, trade_date_str, traders):
    """Add trader match + avak lookup validation to extracted PDF rows."""
    from accounts.pdf_trader_match import resolve_trader_for_pdf_row

    try:
        trade_date = date.fromisoformat(trade_date_str) if trade_date_str else None
    except ValueError:
        trade_date = None

    avak_map = {}
    if trade_date:
        for avak in Avak.objects.filter(date=trade_date, is_cancelled=False):
            avak_map[str(avak.lot_number).strip()] = avak
            avak_map[avak.lot_number.strip().lstrip("0") or "0"] = avak

    enriched = []
    for row in rows:
        item = dict(row)
        item["errors"] = []

        lot_number = str(item.get("lot_number") or "").strip()
        if not lot_number:
            item["errors"].append("Lot No is required.")

        price = _to_decimal(item.get("trade_price"), default="0")
        if price <= 0:
            item["errors"].append("Trade Price is missing or invalid.")

        bags = _to_int(item.get("no_of_bags"), default=-1)
        if bags < 0:
            item["errors"].append("Bags count is invalid.")

        trader, _note = resolve_trader_for_pdf_row(item, traders)
        if trader:
            item["buyer_id"] = trader.id
            item["buyer_code"] = trader.short_code or trader.name
            item["trader_matched"] = True
        else:
            item["buyer_id"] = None
            if not item.get("buyer_code"):
                item["buyer_code"] = ""
            item["trader_matched"] = False
            item["errors"].append(
                "Buyer not found. Select Buyer Code from the dropdown."
            )

        if lot_number:
            avak = avak_map.get(lot_number) or avak_map.get(
                lot_number.lstrip("0") or "0"
            )
            if avak:
                item["avak_id"] = avak.id
                item["avak_found"] = True
            else:
                item["avak_id"] = None
                item["avak_found"] = False
                if trade_date:
                    item["errors"].append(
                        f"No AVAK record for lot {lot_number} on "
                        f"{trade_date.strftime('%d-%m-%Y')}."
                    )

        item["valid"] = not item["errors"]
        enriched.append(item)

    return enriched, trade_date


def _traders_for_pdf_json(traders):
    return [
        {
            "id": t.id,
            "code": t.short_code or t.name,
            "name": t.name,
        }
        for t in traders
    ]


def _available_lots_for_date(trade_date):
    if not trade_date:
        return []
    return list(
        Avak.objects.filter(date=trade_date, is_cancelled=False)
        .order_by("lot_number")
        .values_list("lot_number", flat=True)
    )


@login_required
def upload_tender_pdf(request):
    """Extract tender data from APMC PDF and return preview payload."""
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST request expected."})

    pdf_file = request.FILES.get("pdf_file")
    if not pdf_file:
        return JsonResponse({"success": False, "message": "Please select a PDF file to upload."})

    if not pdf_file.name.lower().endswith(".pdf"):
        return JsonResponse({"success": False, "message": "Only PDF files are allowed."})

    if pdf_file.size > 10 * 1024 * 1024:
        return JsonResponse({"success": False, "message": "PDF file size must be under 10 MB."})

    from accounts.apmc_pdf_extractor import extract_apmc_tender_pdf

    try:
        parsed = extract_apmc_tender_pdf(pdf_file)
    except RuntimeError as exc:
        return JsonResponse({"success": False, "message": str(exc)})
    except Exception:
        return JsonResponse({
            "success": False,
            "message": "Failed to read PDF. Please upload a valid APMC tender PDF.",
        })

    if not parsed.get("success"):
        return JsonResponse({
            "success": False,
            "message": " ".join(parsed.get("errors") or ["PDF extraction failed."]),
            "errors": parsed.get("errors") or [],
        })

    trade_date_str = parsed.get("trade_date")
    if not trade_date_str:
        return JsonResponse({
            "success": False,
            "message": "Trade Date could not be extracted from the PDF.",
        })

    traders = list(Trader.objects.all().order_by("name"))
    rows, trade_date = _enrich_pdf_rows_for_preview(parsed["rows"], trade_date_str, traders)

    valid_count = sum(1 for r in rows if r["valid"])
    invalid_count = len(rows) - valid_count

    return JsonResponse({
        "success": True,
        "message": f"Extracted {len(rows)} lot(s) from PDF.",
        "trade_date": trade_date_str,
        "rows": rows,
        "traders": _traders_for_pdf_json(traders),
        "available_lots": _available_lots_for_date(trade_date),
        "summary": {
            "total": len(rows),
            "valid": valid_count,
            "invalid": invalid_count,
        },
        "parse_warnings": parsed.get("errors") or [],
    })


@login_required
def validate_tender_pdf_rows(request):
    """Re-validate edited preview rows (after user corrections)."""
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST request expected."})

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "Invalid JSON."})

    trade_date_str = (data.get("trade_date") or "").strip()
    rows = data.get("rows") or []
    if not trade_date_str:
        return JsonResponse({"success": False, "message": "Trade Date is required."})

    traders = list(Trader.objects.all().order_by("name"))
    enriched, trade_date = _enrich_pdf_rows_for_preview(rows, trade_date_str, traders)
    valid_count = sum(1 for r in enriched if r["valid"])

    return JsonResponse({
        "success": True,
        "trade_date": trade_date_str,
        "rows": enriched,
        "available_lots": _available_lots_for_date(trade_date),
        "summary": {
            "total": len(enriched),
            "valid": valid_count,
            "invalid": len(enriched) - valid_count,
        },
    })


@login_required
def confirm_tender_pdf_import(request):
    """Validate preview rows and update AVAK rate + buyer from PDF data."""
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST request expected."})

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "Invalid JSON."})

    trade_date_str = (data.get("trade_date") or "").strip()
    rows = data.get("rows") or []

    if not trade_date_str:
        return JsonResponse({"success": False, "message": "Trade Date is required."})
    try:
        trade_date = date.fromisoformat(trade_date_str)
    except ValueError:
        return JsonResponse({"success": False, "message": "Invalid Trade Date."})

    if not rows:
        return JsonResponse({"success": False, "message": "No lot data to import."})

    traders = list(Trader.objects.all().order_by("name"))
    enriched, _ = _enrich_pdf_rows_for_preview(rows, trade_date_str, traders)

    invalid = [r for r in enriched if not r["valid"]]
    if invalid:
        messages_list = []
        for r in invalid[:5]:
            lot = r.get("lot_number") or r.get("lot_code") or "?"
            err = "; ".join(r.get("errors") or ["Invalid row"])
            messages_list.append(f"Lot {lot}: {err}")
        extra = len(invalid) - 5
        if extra > 0:
            messages_list.append(f"...and {extra} more invalid row(s).")
        return JsonResponse({
            "success": False,
            "message": "Validation failed. Please fix errors before confirming.",
            "errors": messages_list,
            "invalid_rows": invalid,
        })

    updated = 0
    with transaction.atomic():
        for row in enriched:
            avak = Avak.objects.filter(
                date=trade_date,
                lot_number__iexact=str(row["lot_number"]),
                is_cancelled=False,
            ).first()
            if not avak:
                lot_num = str(row["lot_number"]).lstrip("0") or "0"
                avak = Avak.objects.filter(
                    date=trade_date,
                    lot_number__iexact=lot_num,
                    is_cancelled=False,
                ).first()
            if not avak:
                return JsonResponse({
                    "success": False,
                    "message": f"AVAK lot {row['lot_number']} not found for {trade_date_str}.",
                })

            avak.rate = _to_decimal(row.get("trade_price"), default="0")
            buyer_id = row.get("buyer_id")
            avak.buyer_id = int(buyer_id) if buyer_id else None
            bags = _to_int(row.get("no_of_bags"), default=0)
            if bags >= 0:
                avak.no_of_bags = bags
            avak.save(update_fields=["rate", "buyer_id", "no_of_bags"])
            updated += 1

    return JsonResponse({
        "success": True,
        "message": f"Successfully updated {updated} AVAK record(s) from PDF.",
        "updated": updated,
        "trade_date": trade_date_str,
    })


@login_required
def save_tender_rates(request):
    """Persist tender form rates to avak lots for a date."""
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST request expected."})

    import json

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "Invalid JSON."})

    entry_date = (data.get("date") or "").strip()
    rates = data.get("rates") or {}
    if not entry_date:
        return JsonResponse({"success": False, "message": "Date is required."})

    updated = 0
    for lot_no, rate_val in rates.items():
        lot_no = str(lot_no).strip()
        if not lot_no:
            continue
        count = Avak.objects.filter(
            date=entry_date, lot_number__iexact=lot_no, is_cancelled=False
        ).update(rate=_to_decimal(rate_val, default="0"))
        updated += count

    return JsonResponse({"success": True, "updated": updated})


@login_required
def get_lot_bags_details(request):
    from accounts.models import BikriBagWeight, BagTransfer

    d = request.GET.get("date")
    lot_no = (request.GET.get("lot_no") or "").strip()
    if not (d and lot_no):
        return JsonResponse({"success": False, "message": "Missing date or lot number."})

    avak = Avak.objects.filter(
        date=d, lot_number__iexact=lot_no, is_cancelled=False
    ).select_related("buyer").first()
    if not avak:
        return JsonResponse({"success": False, "message": "No avak lot found for this date."})

    # Match by avak arrival date (same date picker as get_lots_by_date), not bikri.date
    bikris = Bikri.objects.filter(
        avak=avak, is_cancelled=False
    ).select_related("buyer")
    if not bikris.exists():
        rate, buyer_code, buyer_id = _avak_tender_fields(avak)
        default_avg = float(DEFAULT_AVG_BAG_WEIGHT_KG)
        return JsonResponse({
            "success": True,
            "pending_bikri": True,
            "message": (
                "Avak entry found. Showing estimated weight using avak bags "
                f"× {DEFAULT_AVG_BAG_WEIGHT_KG} kg. Complete Bikri entry for actual weights."
            ),
            "rate": rate,
            "buyer_code": buyer_code,
            "buyer_id": buyer_id,
            "avg_weight": default_avg,
            "default_avg_bag_weight": default_avg,
            "avak_bags": avak.no_of_bags,
            "bags": [],
            "transfers": [],
        })
    
    # Calculate average weight
    total_bags_count = sum(b.no_of_bags for b in bikris)
    total_weight_sum = sum(b.total_weight for b in bikris)
    avg_weight = total_weight_sum / total_bags_count if total_bags_count > 0 else Decimal("0")
    
    bags = []
    bag_weights = BikriBagWeight.objects.filter(bikri__in=bikris).order_by("bag_no")
    for bw in bag_weights:
        bags.append({
            "id": bw.id,
            "bag_no": bw.bag_no,
            "weight": float(bw.weight),
            "trader": bw.bikri.buyer.short_code or bw.bikri.buyer.name
        })

    # Fetch transferred bags
    transfers_data = []
    transfers = BagTransfer.objects.filter(source_bikri__in=bikris).select_related('target_buyer')
    for t in transfers:
        transfers_data.append({
            "trader": t.target_buyer.short_code or t.target_buyer.name,
            "bags": t.no_of_bags,
            "weight": float(t.total_weight)
        })
    
    # First active bikri for rate and buyer_code; prefer avak tender fields when set
    first_b = bikris.first()
    avak_rate, avak_buyer_code, avak_buyer_id = _avak_tender_fields(avak)
    rate = avak_rate if avak_rate else float(first_b.rate)
    buyer_id = avak_buyer_id or first_b.buyer_id
    buyer_code = avak_buyer_code or (first_b.buyer.short_code or first_b.buyer.name)

    return JsonResponse({
        "success": True,
        "pending_bikri": False,
        "rate": rate,
        "buyer_code": buyer_code,
        "buyer_id": buyer_id,
        "avg_weight": float(avg_weight),
        "default_avg_bag_weight": float(DEFAULT_AVG_BAG_WEIGHT_KG),
        "avak_bags": avak.no_of_bags,
        "bags": bags,
        "transfers": transfers_data,
    })


@login_required
def transfer_bag_weights(request):
    """
    Ownership reassignment only — uses BagTransfer model.
    - Creates a BagTransfer record (target buyer billing, no lot number).
    - Reduces source Bikri bags/weight/amounts in-place.
    - Avak is NEVER touched.
    - No new Avak or Bikri records created.
    """
    if request.method == "POST":
        import json
        from django.db import transaction
        from accounts.models import BagTransfer

        try:
            data = json.loads(request.body)
            selected_bag_ids = data.get("selected_bag_ids", [])
            target_buyer_id = data.get("target_buyer_id")

            if not (selected_bag_ids and target_buyer_id):
                return JsonResponse({"success": False, "message": "Missing required data."})

            with transaction.atomic():
                # 1. Fetch selected BikriBagWeight records
                bag_weights_qs = BikriBagWeight.objects.filter(
                    id__in=selected_bag_ids
                ).select_related("bikri", "bikri__avak", "bikri__buyer")

                if not bag_weights_qs.exists():
                    return JsonResponse({"success": False, "message": "No valid bags found."})

                first_bw = bag_weights_qs.first()
                source_bikri = first_bw.bikri
                # ← Avak is never referenced or modified

                # 2. Get target buyer
                target_buyer = Trader.objects.get(id=target_buyer_id)

                # 3. Compute transfer totals
                transferred_bags   = bag_weights_qs.count()
                transferred_weight = sum(bw.weight for bw in bag_weights_qs)
                rate               = source_bikri.rate
                transferred_amount = (transferred_weight / Decimal("100")) * rate

                # Billing calculations using source bikri's rates
                t_hamali   = transferred_bags * source_bikri.hamali_rate
                t_packing  = transferred_bags * source_bikri.packing_rate
                t_dalali   = transferred_amount * (source_bikri.dalali_rate / Decimal("100"))
                t_cess     = transferred_amount * (source_bikri.cess_rate / Decimal("100"))
                t_gst      = (transferred_amount + t_dalali) * (source_bikri.gst_rate / Decimal("100"))
                t_weighman = transferred_bags * Decimal("1.00")
                t_total    = transferred_amount + t_hamali + t_packing + t_dalali + t_cess + t_gst + t_weighman

                # 4. Create BagTransfer - target buyer's billing record with NO lot number
                bag_transfer = BagTransfer.objects.create(
                    date=source_bikri.date,
                    source_bikri=source_bikri,
                    target_buyer=target_buyer,
                    no_of_bags=transferred_bags,
                    total_weight=transferred_weight,
                    rate=rate,
                    amount=transferred_amount,
                    hamali=t_hamali,
                    packing=t_packing,
                    dalali=t_dalali,
                    cess=t_cess,
                    gst=t_gst,
                    weighman_fee=t_weighman,
                    total_amount=t_total,
                    hamali_rate=source_bikri.hamali_rate,
                    packing_rate=source_bikri.packing_rate,
                    dalali_rate=source_bikri.dalali_rate,
                    cess_rate=source_bikri.cess_rate,
                    gst_rate=source_bikri.gst_rate,
                    weighman_fee_rate=Decimal("1.00"),
                )

                from accounts.models import BagTransferWeight
                for i, bw in enumerate(bag_weights_qs, 1):
                    BagTransferWeight.objects.create(
                        transfer=bag_transfer,
                        bag_no=i,
                        weight=bw.weight
                    )

                # 4.5 Delete the transferred individual bags so they disappear from lot-detail-modification
                bag_weights_qs.delete()

                # 4.6 Re-number remaining bags in source_bikri
                remaining_bags = BikriBagWeight.objects.filter(bikri=source_bikri).order_by("bag_no", "id")
                for i, bw in enumerate(remaining_bags, 1):
                    if bw.bag_no != i:
                        bw.bag_no = i
                        bw.save()

                # 5. Reduce source Bikri in-place — bags and all financial totals
                new_bags   = max(0, source_bikri.no_of_bags - transferred_bags)
                new_weight = max(Decimal("0.000"), source_bikri.total_weight - transferred_weight)
                new_amount = (new_weight / Decimal("100")) * rate

                source_bikri.no_of_bags   = new_bags
                source_bikri.total_weight = new_weight
                source_bikri.amount       = new_amount
                source_bikri.hamali       = new_bags * source_bikri.hamali_rate
                source_bikri.packing      = new_bags * source_bikri.packing_rate
                source_bikri.dalali       = new_amount * (source_bikri.dalali_rate / Decimal("100"))
                source_bikri.cess         = new_amount * (source_bikri.cess_rate / Decimal("100"))
                source_bikri.gst          = (new_amount + source_bikri.dalali) * (source_bikri.gst_rate / Decimal("100"))
                source_bikri.weighman_fee = new_bags * Decimal("1.00")
                source_bikri.total_amount = (
                    new_amount + source_bikri.hamali + source_bikri.packing
                    + source_bikri.dalali + source_bikri.cess
                    + source_bikri.gst + source_bikri.weighman_fee
                )
                source_bikri.farmer_hamali  = new_bags * source_bikri.farmer_hamali_rate
                source_bikri.farmer_packing = new_bags * source_bikri.farmer_packing_rate
                source_bikri.unload_fee     = new_bags * source_bikri.farmer_unloading_rate
                source_bikri.net_payable    = new_amount - source_bikri.farmer_hamali - source_bikri.farmer_packing - source_bikri.unload_fee

                if new_bags == 0:
                    source_bikri.is_cancelled = True
                source_bikri.save()

                # 6. Reduce Avak in-place
                avak = source_bikri.avak
                avak.no_of_bags = new_bags
                avak.hamali_total = new_bags * avak.hamali_rate
                if new_bags == 0:
                    avak.is_cancelled = True
                avak.save()

                return JsonResponse({
                    "success": True,
                    "message": (
                        f"Transferred {transferred_bags} bag(s) to "
                        f"{target_buyer.short_code or target_buyer.name}. "
                        f"Source updated. Avak unchanged."
                    )
                })

        except Exception as e:
            return JsonResponse({"success": False, "message": f"Error: {str(e)}"})

    return JsonResponse({"success": False, "message": "POST request expected."})


# --- ACCOUNTS MODULE VIEWS ---


@login_required
def payment_list(request):
    from .models import LedgerAccount as _LedgerAccount
    from_date = request.GET.get("from_date", "")
    to_date   = request.GET.get("to_date", "")
    today_str = date.today().strftime("%Y-%m-%d")
    if from_date or to_date:
        payments = FinancialTransaction.objects.filter(transaction_type="Debit").order_by("-date", "-created_at")
        if from_date:
            payments = payments.filter(date__gte=from_date)
        if to_date:
            payments = payments.filter(date__lte=to_date)
    else:
        payments = FinancialTransaction.objects.none()
    cash_bank_accounts = _cash_bank_ledgers_qs()
    return render(request, "accounts/payment_list.html", {
        "payments": payments,
        "from_date": from_date,
        "to_date": to_date,
        "today": today_str,
        "farmers": Farmer.objects.all().order_by("name"),
        "cash_bank_accounts": cash_bank_accounts,
    })


@login_required
def add_payment(request):
    from .models import LedgerAccount as _LedgerAccount
    cash_bank_accounts = _cash_bank_ledgers_qs()
    # All ledger accounts for debit side (bank-to-bank transfers, etc.)
    all_ledger_accounts = _LedgerAccount.objects.select_related("group").order_by("group__nature", "name")

    # Voucher type → transaction_type + payment_method mapping
    VOUCHER_MAP = {
        "Cash Payment":     ("Debit",  "Cash"),
        "Cheque Payment":   ("Debit",  "Cheque"),
        "NEFT/RTGS Payment":("Debit",  "NEFT"),
        "Cash Receipt":     ("Credit", "Cash"),
        "Cheque Receipt":   ("Credit", "Cheque"),
        "NEFT/RTGS Receipt":("Credit", "NEFT"),
        "Journal":          ("Debit",  "Others"),
    }

    if request.method == "POST":
        date_str = request.POST.get("date")
        voucher_type = request.POST.get("voucher_type", "Cash Payment")
        transaction_type, payment_method = VOUCHER_MAP.get(voucher_type, ("Debit", "Cash"))

        person_type = request.POST.get("person_type")
        farmer_id = request.POST.get("farmer_id")
        trader_id = request.POST.get("trader_id")
        name = request.POST.get("name")
        place = request.POST.get("place")
        phone_number = request.POST.get("phone_number")
        bikri_bill_no = request.POST.get("bikri_bill_no")
        pay_from_ledger_id = request.POST.get("pay_from_ledger_id")  # credit account (bank paying out)
        debit_ledger_id = request.POST.get("debit_ledger_id")         # debit account (for bank-to-bank)
        cheque_no = request.POST.get("cheque_no", "").strip() or None
        cheque_bank_name = request.POST.get("cheque_bank_name", "").strip() or None
        narration = request.POST.get("narration")
        amount = _to_decimal(request.POST.get("amount"), default="0")

        # Validate ledger ids
        if pay_from_ledger_id and pay_from_ledger_id.isdigit():
            try:
                ledger_obj = _LedgerAccount.objects.get(id=int(pay_from_ledger_id))
                name_lower = ledger_obj.name.lower()
                if "cash" in name_lower:
                    payment_method = "Cash"
                elif "cheque" in name_lower:
                    payment_method = "Cheque"
                elif "rtgs" in name_lower or "neft" in name_lower:
                    payment_method = "NEFT"
            except _LedgerAccount.DoesNotExist:
                pay_from_ledger_id = None

        if debit_ledger_id and not debit_ledger_id.isdigit():
            debit_ledger_id = None

        FinancialTransaction.objects.create(
            date=date_str or date.today(),
            transaction_type=transaction_type,
            voucher_type=voucher_type,
            person_type=person_type,
            farmer_id=int(farmer_id) if farmer_id and farmer_id.isdigit() else None,
            trader_id=int(trader_id) if trader_id and trader_id.isdigit() else None,
            name=name,
            place=place,
            phone_number=phone_number,
            bikri_bill_no=bikri_bill_no,
            payment_method=payment_method,
            pay_from_ledger_id=int(pay_from_ledger_id) if pay_from_ledger_id and pay_from_ledger_id.isdigit() else None,
            debit_ledger_id=int(debit_ledger_id) if debit_ledger_id and debit_ledger_id.isdigit() else None,
            cheque_no=cheque_no,
            cheque_bank_name=cheque_bank_name,
            narration=narration,
            amount=amount,
        )
        messages.success(request, f"{voucher_type} entry saved successfully.")
        entry_date = date_str or date.today().strftime("%Y-%m-%d")
        from django.urls import reverse as _reverse
        if transaction_type == "Debit":
            return redirect(_reverse("payment_list") + f"?from_date={entry_date}&to_date={entry_date}")
        else:
            return redirect(_reverse("receipt_list") + f"?from_date={entry_date}&to_date={entry_date}")

    return render(
        request,
        "accounts/payment_form.html",
        {
            "today": date.today().strftime("%Y-%m-%d"),
            "farmers": Farmer.objects.all(),
            "traders": Trader.objects.all(),
            "cash_bank_accounts": cash_bank_accounts,
            "all_ledger_accounts": all_ledger_accounts,
            "is_payment": True,
            "default_voucher_type": "Cash Payment",
        },
    )


@login_required
def receipt_list(request):
    from .models import LedgerAccount as _LedgerAccount

    # ── Date filter ──
    from_date = request.GET.get("from_date", "")
    to_date   = request.GET.get("to_date", "")
    today_str = date.today().strftime("%Y-%m-%d")

    if from_date or to_date:
        receipts = FinancialTransaction.objects.filter(transaction_type="Credit").order_by("-date", "-created_at")
        if from_date:
            receipts = receipts.filter(date__gte=from_date)
        if to_date:
            receipts = receipts.filter(date__lte=to_date)
    else:
        receipts = FinancialTransaction.objects.none()

    cash_bank_accounts = _cash_bank_ledgers_qs()

    return render(request, "accounts/receipt_list.html", {
        "receipts": receipts,
        "from_date": from_date,
        "to_date": to_date,
        "today": today_str,
        "traders": Trader.objects.all().order_by("name"),
        "cash_bank_accounts": cash_bank_accounts,
        "default_voucher_type": "Cash Receipt",
        "transaction": {},
    })


@login_required
def add_receipt(request):
    from .models import LedgerAccount as _LedgerAccount

    if request.method == "POST":
        date_str     = request.POST.get("date")
        voucher_type = request.POST.get("voucher_type", "Cash Receipt")
        trader_id    = request.POST.get("trader_id")
        name         = request.POST.get("name")
        place        = request.POST.get("place")
        phone_number = request.POST.get("phone_number")
        pay_from_ledger_id = request.POST.get("pay_from_ledger_id")
        cheque_no    = request.POST.get("cheque_no", "").strip() or None
        cheque_bank_name = request.POST.get("cheque_bank_name", "").strip() or None
        narration    = request.POST.get("narration")
        amount       = _to_decimal(request.POST.get("amount"), default="0")

        payment_method = "Cash"
        if "Cheque" in voucher_type:
            payment_method = "Cheque"
        elif "NEFT" in voucher_type or "RTGS" in voucher_type:
            payment_method = "NEFT"

        if pay_from_ledger_id and not pay_from_ledger_id.isdigit():
            pay_from_ledger_id = None

        FinancialTransaction.objects.create(
            date=date_str or date.today(),
            transaction_type="Credit",
            voucher_type=voucher_type,
            person_type="Trader",
            trader_id=int(trader_id) if trader_id and trader_id.isdigit() else None,
            name=name,
            place=place,
            phone_number=phone_number,
            payment_method=payment_method,
            pay_from_ledger_id=int(pay_from_ledger_id) if pay_from_ledger_id and pay_from_ledger_id.isdigit() else None,
            cheque_no=cheque_no,
            cheque_bank_name=cheque_bank_name,
            narration=narration,
            amount=amount,
        )
        messages.success(request, f"{voucher_type} entry saved successfully.")
        today_str = (date_str or date.today().strftime("%Y-%m-%d"))
        from django.urls import reverse
        return redirect(reverse("receipt_list") + f"?from_date={today_str}&to_date={today_str}")

    cash_bank_accounts = _cash_bank_ledgers_qs()

    return render(
        request,
        "accounts/receipt_form.html",
        {
            "today": date.today().strftime("%Y-%m-%d"),
            "traders": Trader.objects.all().order_by("name"),
            "cash_bank_accounts": cash_bank_accounts,
            "is_payment": False,
            "default_voucher_type": "Cash Receipt",
            "transaction": {},
        },
    )


@login_required
def farmer_ledger(request):
    farmer_id = request.GET.get("farmer_id")
    from_date = request.GET.get("from_date", "")
    to_date = request.GET.get("to_date", "")

    farmer = None
    ledger_entries = []
    summary = {
        "total_debit": Decimal("0"),
        "total_credit": Decimal("0"),
        "balance": Decimal("0"),
        "balance_side": "Cr",
    }

    if farmer_id:
        farmer = get_object_or_404(Farmer, id=farmer_id)

        opening_balance = farmer.opening_balance
        balance_type = farmer.balance_type  # 'Cr' or 'Dr'

        # ── Bikri Entries: grouped by date → one bill entry per date ────────
        bikris = (
            Bikri.objects.select_related("avak")
            .filter(avak__farmer=farmer, is_cancelled=False)
            .order_by("date", "id")
        )
        if from_date:
            bikris = bikris.filter(date__gte=from_date)
        if to_date:
            bikris = bikris.filter(date__lte=to_date)

        # Group by date
        from collections import defaultdict
        date_groups = defaultdict(list)
        for b in bikris:
            date_groups[b.date].append(b)

        bill_counter = 1
        for sale_date in sorted(date_groups.keys()):
            group = date_groups[sale_date]
            total_net   = _calc_group_net_payable(group, sale_date)
            total_bags  = sum(b.no_of_bags for b in group)
            total_wt    = sum(b.total_weight for b in group)
            lot_nos     = ", ".join(str(b.avak.lot_number) for b in group)
            bikri_ids   = [b.id for b in group]
            # Use the stored bill_no (same value as patti_no in view_bikri)
            patti_no    = (group[0].bill_no or "").strip() or str(bill_counter)
            bill_no     = patti_no
            bill_counter += 1

            narration = (
                f"ವಿಕ್ರಿ ಪಟ್ಟಿ | "
                f"{len(group)} lot(s) | {total_bags} bags | {total_wt} qtl"
            )
            ledger_entries.append({
                "date":       sale_date,
                "narration":  narration,
                "lot_nos":    lot_nos,
                "bill_no":    bill_no,
                "bikri_ids":  bikri_ids,
                "ref_type":   "bikri",
                "method":     "",
                "debit":      Decimal("0"),
                "credit":     total_net,
            })

        # ── Financial Transactions ────────────────────────────────────────────
        transactions = FinancialTransaction.objects.filter(farmer=farmer)
        if from_date:
            transactions = transactions.filter(date__gte=from_date)
        if to_date:
            transactions = transactions.filter(date__lte=to_date)

        for t in transactions:
            # Payment to farmer = Debit (reduces the amount we owe)
            # Receipt from farmer = Credit (farmer gives us back money)
            debit  = _quantize_money(t.amount) if t.transaction_type == "Debit"  else Decimal("0")
            credit = _quantize_money(t.amount) if t.transaction_type == "Credit" else Decimal("0")
            narration = t.narration or (
                f"Payment – {t.payment_method}" if t.transaction_type == "Debit"
                else f"Receipt – {t.payment_method}"
            )
            if t.bikri_bill_no:
                narration += f" | Bill: {t.bikri_bill_no}"
            ledger_entries.append({
                "date": t.date,
                "narration": narration,
                "ref": t.payment_method or "",
                "ref_id": t.id,
                "ref_type": "txn",
                "method": t.payment_method or "",
                "debit": debit,
                "credit": credit,
            })

        # ── Sort by date ──────────────────────────────────────────────────────
        ledger_entries.sort(key=lambda x: x["date"])

        # ── Running Balance ───────────────────────────────────────────────────
        # Positive = we owe farmer (Cr), Negative = farmer owes us (Dr)
        if balance_type == "Cr":
            running = opening_balance
        else:
            running = -opening_balance

        for entry in ledger_entries:
            running += entry["credit"] - entry["debit"]
            entry["running"] = abs(running)
            entry["running_side"] = "Cr" if running >= 0 else "Dr"
            summary["total_debit"]  += entry["debit"]
            summary["total_credit"] += entry["credit"]

        summary["balance"] = abs(running)
        summary["balance_side"] = "Cr" if running >= 0 else "Dr"

    return render(
        request,
        "accounts/farmer_ledger.html",
        {
            "farmer": farmer,
            "farmers": Farmer.objects.order_by("name").all(),
            "ledger_entries": ledger_entries,
            "summary": summary,
            "from_date": from_date,
            "to_date": to_date,
            "today": date.today().strftime("%Y-%m-%d"),
        },
    )


@login_required
def trader_ledger(request):
    trader_id = request.GET.get("trader_id")
    from_date = request.GET.get("from_date")
    to_date = request.GET.get("to_date")

    trader = None
    ledger_entries = []
    summary = {"total_debit": Decimal("0"), "total_credit": Decimal("0"), "balance": Decimal("0")}

    if trader_id:
        trader = get_object_or_404(Trader, id=trader_id)

        # 1. Opening Balance
        opening_balance = trader.opening_balance
        balance_type = trader.balance_type

        # Dictionary to group entries by (date, bill_id)
        grouped_entries = {}

        # 2. Trader Bills (Debit for Trader)
        bills = TraderBill.objects.filter(buyer=trader)
        if from_date:
            bills = bills.filter(date__gte=from_date)
        if to_date:
            bills = bills.filter(date__lte=to_date)

        for b in bills:
            # We don't have a direct link yet, but we can try to match or just show separately
            # If we want to merge, we need a common ID. TraderBill ID works.
            key = (b.date, str(b.id))
            grouped_entries[key] = {
                "date": b.date,
                "remarks": f"ಖರೀದಿ ಪಟ್ಟಿ – #{b.invoice_no}",
                "bill_no": b.invoice_no,
                "method": "-",
                "debit": b.grand_total,
                "credit": Decimal("0"),
                "type": "bill",
            }

        # 3. Financial Transactions (Credit if Receipt, Debit if Payment)
        transactions = FinancialTransaction.objects.filter(trader=trader)
        if from_date:
            transactions = transactions.filter(date__gte=from_date)
        if to_date:
            transactions = transactions.filter(date__lte=to_date)

        for t in transactions:
            debit = t.amount if t.transaction_type == "Debit" else Decimal("0")
            credit = t.amount if t.transaction_type == "Credit" else Decimal("0")
            
            # Handle COMBINED bill IDs
            bill_ids = []
            if t.bikri_bill_no:
                if t.bikri_bill_no.startswith("COMBINED:"):
                    bill_ids = t.bikri_bill_no.replace("COMBINED:", "").split(",")
                else:
                    bill_ids = [t.bikri_bill_no]

            merged = False
            for bid in bill_ids:
                key = (t.date, bid)
                if key in grouped_entries:
                    entry = grouped_entries[key]
                    entry["debit"] += debit
                    entry["credit"] += credit
                    if t.payment_method and t.payment_method != "-":
                        entry["method"] = t.payment_method
                    if t.narration:
                        entry["remarks"] = t.narration
                    merged = True
                    break

            if not merged:
                ledger_entries.append(
                    {
                        "date": t.date,
                        "remarks": t.narration or f"{t.transaction_type} entry",
                        "method": t.payment_method,
                        "debit": debit,
                        "credit": credit,
                        "type": "transaction",
                    }
                )
        
        # Add the grouped/merged entries to the list
        ledger_entries.extend(grouped_entries.values())

        # Sort by date
        ledger_entries.sort(key=lambda x: x["date"])

        # Calculate running balance (Debit - Credit for Trader)
        current_balance = opening_balance if balance_type == "Dr" else -opening_balance
        for entry in ledger_entries:
            current_balance += entry["debit"] - entry["credit"]
            entry["balance"] = current_balance
            summary["total_debit"] += entry["debit"]
            summary["total_credit"] += entry["credit"]

        summary["balance"] = current_balance

    return render(
        request,
        "accounts/trader_ledger.html",
        {
            "trader": trader,
            "traders": Trader.objects.all(),
            "ledger_entries": ledger_entries,
            "summary": summary,
            "from_date": from_date,
            "to_date": to_date,
        },
    )


@login_required
def search_bikri(request):
    q = request.GET.get("q", "")
    farmer_id = request.GET.get("farmer_id")
    trader_id = request.GET.get("trader_id")
    selected_date = request.GET.get("date")

    filters = Q()
    if q:
        filters &= (
            Q(id__icontains=q)
            | Q(avak__lot_number__icontains=q)
            | Q(avak__farmer__name__icontains=q)
            | Q(buyer__name__icontains=q)
        )

    if farmer_id:
        filters &= Q(avak__farmer_id=farmer_id)
    if trader_id:
        filters &= Q(buyer_id=trader_id)
    if selected_date:
        filters &= Q(date=selected_date)

    bikris = (
        Bikri.objects.filter(filters)
        .select_related("avak__farmer", "buyer")
        .order_by("-date")[:20]
    )


    results = []
    
    # Add "Combined Bill" option if multiple bills exist for this person/date
    if bikris.count() > 1 and (farmer_id or trader_id) and selected_date:
        total_np = sum(b.net_payable for b in bikris)
        total_ta = sum(b.total_amount for b in bikris)
        lot_nos = ", ".join([b.avak.lot_number for b in bikris])
        bill_ids = ",".join([str(b.id) for b in bikris])
        results.append({
            "id": f"COMBINED:{bill_ids}",
            "text": f"Lots {lot_nos}",
            "net_payable": str(total_np),
            "total_amount": str(total_ta),
        })

    for b in bikris:
        results.append(
            {
                "id": b.id,
                "text": f"Bill #{b.id} - Lot {b.avak.lot_number} - {b.buyer.short_code or b.buyer.name}",
                "net_payable": str(b.net_payable),
                "total_amount": str(b.total_amount),
            }
        )
    return JsonResponse({"results": results})


@login_required
def edit_financial_transaction(request, transaction_id):
    transaction = get_object_or_404(FinancialTransaction, id=transaction_id)
    from .models import LedgerAccount as _LedgerAccount

    VOUCHER_MAP = {
        "Cash Payment":     ("Debit",  "Cash"),
        "Cheque Payment":   ("Debit",  "Cheque"),
        "NEFT/RTGS Payment":("Debit",  "NEFT"),
        "Cash Receipt":     ("Credit", "Cash"),
        "Cheque Receipt":   ("Credit", "Cheque"),
        "NEFT/RTGS Receipt":("Credit", "NEFT"),
        "Journal":          ("Debit",  "Others"),
    }

    if request.method == "POST":
        transaction.date = request.POST.get("date") or transaction.date

        voucher_type = request.POST.get("voucher_type", transaction.voucher_type or "Cash Payment")
        transaction.voucher_type = voucher_type
        t_type, pay_method = VOUCHER_MAP.get(voucher_type, (transaction.transaction_type, transaction.payment_method))
        transaction.transaction_type = t_type
        transaction.payment_method = pay_method

        transaction.person_type = request.POST.get("person_type") or transaction.person_type

        farmer_id = request.POST.get("farmer_id")
        trader_id = request.POST.get("trader_id")
        transaction.farmer_id = int(farmer_id) if farmer_id and farmer_id.isdigit() else None
        transaction.trader_id = int(trader_id) if trader_id and trader_id.isdigit() else None

        transaction.name = request.POST.get("name")
        transaction.place = request.POST.get("place")
        transaction.phone_number = request.POST.get("phone_number")
        transaction.bikri_bill_no = request.POST.get("bikri_bill_no")
        transaction.narration = request.POST.get("narration")
        transaction.amount = _to_decimal(request.POST.get("amount"), default="0")

        pay_from_ledger_id = request.POST.get("pay_from_ledger_id")
        transaction.pay_from_ledger_id = int(pay_from_ledger_id) if pay_from_ledger_id and pay_from_ledger_id.isdigit() else None

        debit_ledger_id = request.POST.get("debit_ledger_id")
        transaction.debit_ledger_id = int(debit_ledger_id) if debit_ledger_id and debit_ledger_id.isdigit() else None

        transaction.cheque_no = request.POST.get("cheque_no", "").strip() or None
        transaction.cheque_bank_name = request.POST.get("cheque_bank_name", "").strip() or None

        transaction.save()
        messages.success(request, "Transaction updated successfully.")
        return (
            redirect("payment_list")
            if transaction.transaction_type == "Debit"
            else redirect("receipt_list")
        )

    # Get Bikri Bill Info for pre-fill if exists
    bikri_bill_text = ""
    if transaction.bikri_bill_no:
        from .models import Bikri
        bill = Bikri.objects.filter(id=transaction.bikri_bill_no).select_related('buyer', 'avak').first()
        if bill:
            bikri_bill_text = f"Bill #{bill.id} - Lot {bill.avak.lot_number} - {bill.buyer.short_code or bill.buyer.name}"

    cash_bank_accounts = _cash_bank_ledgers_qs()
    all_ledger_accounts = _LedgerAccount.objects.select_related("group").order_by("group__nature", "name")

    return render(
        request,
        "accounts/payment_form.html",
        {
            "transaction": transaction,
            "today": transaction.date.strftime("%Y-%m-%d"),
            "farmers": Farmer.objects.all(),
            "traders": Trader.objects.all(),
            "cash_bank_accounts": cash_bank_accounts,
            "all_ledger_accounts": all_ledger_accounts,
            "is_payment": transaction.transaction_type == "Debit",
            "is_edit": True,
            "bikri_bill_text": bikri_bill_text,
            "default_voucher_type": transaction.voucher_type or ("Cash Payment" if transaction.transaction_type == "Debit" else "Cash Receipt"),
        },
    )


@login_required
def delete_financial_transaction(request, transaction_id):
    transaction = get_object_or_404(FinancialTransaction, id=transaction_id)
    t_type = transaction.transaction_type
    transaction.delete()
    messages.success(request, "Transaction deleted successfully.")
    return redirect("payment_list") if t_type == "Debit" else redirect("receipt_list")


@login_required
def view_financial_transaction(request, transaction_id):
    transaction = get_object_or_404(FinancialTransaction, id=transaction_id)
    return render(
        request, "accounts/view_transaction.html", {"transaction": transaction}
    )


# ─────────────────────────────────────────────────────────────────────────────
# TALLY VOUCHER SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

from .models import LedgerGroup, LedgerAccount, Voucher, VoucherLine


# ── LEDGER MASTER ─────────────────────────────────────────────────────────────

@login_required
def ledger_master(request):
    """List all ledger accounts grouped by nature."""
    ensure_default_ledgers()
    groups = LedgerGroup.objects.prefetch_related("ledger_accounts").all()
    return render(request, "accounts/ledger_master.html", {"groups": groups})


@login_required
def add_ledger_account(request):
    ensure_default_ledgers()
    groups = LedgerGroup.objects.all()
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        group_id = request.POST.get("group_id")
        opening_balance = _to_decimal(request.POST.get("opening_balance"), "0")
        balance_type = request.POST.get("balance_type", "Dr")

        if not name:
            messages.error(request, "Account name is required.")
        elif LedgerAccount.objects.filter(name__iexact=name).exists():
            messages.error(request, f'Account "{name}" already exists.')
        else:
            group = get_object_or_404(LedgerGroup, id=group_id)
            LedgerAccount.objects.create(
                name=name,
                group=group,
                opening_balance=opening_balance,
                balance_type=balance_type,
            )
            messages.success(request, f'Ledger "{name}" created successfully.')
            return redirect("ledger_master")

    return render(request, "accounts/add_ledger_account.html", {"groups": groups})


@login_required
def edit_ledger_account(request, ledger_id):
    ledger = get_object_or_404(LedgerAccount, id=ledger_id)
    if ledger.is_system:
        messages.error(request, "System accounts cannot be edited.")
        return redirect("ledger_master")
    ensure_default_ledgers()
    groups = LedgerGroup.objects.all()
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        group_id = request.POST.get("group_id")
        opening_balance = _to_decimal(request.POST.get("opening_balance"), "0")
        balance_type = request.POST.get("balance_type", "Dr")
        if not name:
            messages.error(request, "Account name is required.")
        elif LedgerAccount.objects.filter(name__iexact=name).exclude(id=ledger_id).exists():
            messages.error(request, f'Account "{name}" already exists.')
        else:
            ledger.name = name
            ledger.group = get_object_or_404(LedgerGroup, id=group_id)
            ledger.opening_balance = opening_balance
            ledger.balance_type = balance_type
            ledger.save()
            messages.success(request, "Ledger updated successfully.")
            return redirect("ledger_master")
    return render(request, "accounts/edit_ledger_account.html", {"ledger": ledger, "groups": groups})


@login_required
def delete_ledger_account(request, ledger_id):
    ledger = get_object_or_404(LedgerAccount, id=ledger_id)
    if ledger.is_system:
        messages.error(request, "System accounts cannot be deleted.")
    elif ledger.voucher_lines.exists():
        messages.error(request, "Cannot delete: ledger has voucher entries.")
    else:
        ledger.delete()
        messages.success(request, "Ledger deleted.")
    return redirect("ledger_master")


# ── GROUPING MASTER (Ledger Groups / Categories) ────────────────────────────

@login_required
def grouping_master(request):
    """List all ledger groups — used in Group / Category dropdown."""
    ensure_default_ledgers()
    groups = (
        LedgerGroup.objects.annotate(account_count=Count("ledger_accounts"))
        .order_by("nature", "name")
    )
    nature_order = ["Assets", "Liabilities", "Income", "Expenses"]
    sections = [
        {"nature": nature, "groups": [g for g in groups if g.nature == nature]}
        for nature in nature_order
    ]
    return render(
        request,
        "accounts/grouping_master.html",
        {"groups": groups, "sections": sections, "nature_order": nature_order},
    )


@login_required
def add_ledger_group(request):
    nature_choices = LedgerGroup.NATURE_CHOICES
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        nature = request.POST.get("nature", "").strip()
        if not name:
            messages.error(request, "Group name is required.")
        elif nature not in dict(nature_choices):
            messages.error(request, "Please select a valid category.")
        elif LedgerGroup.objects.filter(name__iexact=name).exists():
            messages.error(request, f'Group "{name}" already exists.')
        else:
            LedgerGroup.objects.create(name=name, nature=nature)
            messages.success(request, f'Group "{name}" created successfully.')
            return redirect("grouping_master")
    return render(
        request,
        "accounts/ledger_group_form.html",
        {"nature_choices": nature_choices, "form_title": "Add Group", "submit_label": "Save"},
    )


@login_required
def edit_ledger_group(request, group_id):
    group = get_object_or_404(LedgerGroup, id=group_id)
    nature_choices = LedgerGroup.NATURE_CHOICES
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        nature = request.POST.get("nature", "").strip()
        if not name:
            messages.error(request, "Group name is required.")
        elif nature not in dict(nature_choices):
            messages.error(request, "Please select a valid category.")
        elif LedgerGroup.objects.filter(name__iexact=name).exclude(id=group_id).exists():
            messages.error(request, f'Group "{name}" already exists.')
        else:
            group.name = name
            group.nature = nature
            group.save()
            messages.success(request, "Group updated successfully.")
            return redirect("grouping_master")
    return render(
        request,
        "accounts/ledger_group_form.html",
        {
            "group": group,
            "nature_choices": nature_choices,
            "form_title": "Edit Group",
            "submit_label": "Update",
        },
    )


@login_required
def delete_ledger_group(request, group_id):
    group = get_object_or_404(LedgerGroup, id=group_id)
    if group.ledger_accounts.exists():
        messages.error(
            request,
            f'Cannot delete "{group.name}": {group.ledger_accounts.count()} ledger account(s) use this group.',
        )
    else:
        name = group.name
        group.delete()
        messages.success(request, f'Group "{name}" deleted.')
    return redirect("grouping_master")


# ── VOUCHER ENTRY ─────────────────────────────────────────────────────────────

def _next_voucher_no(voucher_type):
    """Generate sequential voucher number per type e.g. PAY-0001."""
    prefix_map = {"Payment": "PAY", "Receipt": "REC", "Journal": "JNL", "Contra": "CON"}
    prefix = prefix_map.get(voucher_type, "VCH")
    last = (
        Voucher.objects.filter(voucher_type=voucher_type)
        .order_by("-id")
        .values_list("voucher_no", flat=True)
        .first()
    )
    if last:
        try:
            num = int(last.split("-")[-1]) + 1
        except (ValueError, IndexError):
            num = 1
    else:
        num = 1
    return f"{prefix}-{num:04d}"


def _resolve_ledger_id(lid_str):
    """
    Resolve ledger_id string to an actual LedgerAccount pk.
    Supports: plain int, 'farmer:5', 'trader:3'.
    Auto-creates a linked LedgerAccount for farmer/trader if not yet linked.
    Returns int pk, or None on error.
    """
    if not lid_str:
        return None
    if lid_str.startswith("farmer:"):
        try:
            farmer_id = int(lid_str.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        try:
            farmer = Farmer.objects.get(pk=farmer_id)
        except Farmer.DoesNotExist:
            return None
        try:
            return farmer.ledger_account.pk
        except LedgerAccount.DoesNotExist:
            pass
        group, _ = LedgerGroup.objects.get_or_create(
            name="Sundry Creditors",
            defaults={"nature": "Liabilities"},
        )
        la, _ = LedgerAccount.objects.get_or_create(
            farmer=farmer,
            defaults={"name": farmer.name, "group": group, "balance_type": "Cr"},
        )
        return la.pk
    elif lid_str.startswith("trader:"):
        try:
            trader_id = int(lid_str.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
        try:
            trader = Trader.objects.get(pk=trader_id)
        except Trader.DoesNotExist:
            return None
        try:
            return trader.ledger_account.pk
        except LedgerAccount.DoesNotExist:
            pass
        group, _ = LedgerGroup.objects.get_or_create(
            name="Sundry Debtors",
            defaults={"nature": "Assets"},
        )
        la, _ = LedgerAccount.objects.get_or_create(
            trader=trader,
            defaults={"name": trader.name, "group": group, "balance_type": "Dr"},
        )
        return la.pk
    else:
        try:
            return int(lid_str)
        except (ValueError, TypeError):
            return None


def _ledger_select_value(ledger):
    """Map LedgerAccount to voucher form select value (farmer:/trader:/pk)."""
    if ledger.farmer_id:
        return f"farmer:{ledger.farmer_id}"
    if ledger.trader_id:
        return f"trader:{ledger.trader_id}"
    return str(ledger.pk)


def _parse_voucher_lines_from_post(request):
    """Parse and validate voucher lines from POST. Returns (lines_data, error_msg)."""
    ledger_ids = request.POST.getlist("ledger_id[]")
    entry_types = request.POST.getlist("entry_type[]")
    amounts = request.POST.getlist("amount[]")
    line_narrations = request.POST.getlist("line_narration[]")

    lines_data = []
    total_dr = Decimal("0")
    total_cr = Decimal("0")
    for i in range(len(ledger_ids)):
        raw_lid = ledger_ids[i]
        etype = entry_types[i] if i < len(entry_types) else "Dr"
        amt = _to_decimal(amounts[i] if i < len(amounts) else "0")
        lnar = line_narrations[i] if i < len(line_narrations) else ""
        if not raw_lid or amt <= 0:
            continue
        resolved_lid = _resolve_ledger_id(raw_lid)
        if resolved_lid is None:
            continue
        lines_data.append((resolved_lid, etype, amt, lnar))
        if etype == "Dr":
            total_dr += amt
        else:
            total_cr += amt

    if not lines_data:
        return None, "No valid lines entered."
    if total_dr != total_cr:
        return None, f"Debit total ({total_dr}) ≠ Credit total ({total_cr}). Voucher not balanced."
    return lines_data, None


@login_required
def voucher_list(request):
    voucher_type = request.GET.get("type", "")
    vouchers = Voucher.objects.prefetch_related("lines__ledger")
    if voucher_type:
        vouchers = vouchers.filter(voucher_type=voucher_type)
    return render(request, "accounts/voucher_list.html", {
        "vouchers": vouchers[:200],
        "selected_type": voucher_type,
        "voucher_types": Voucher.VOUCHER_TYPES,
    })


@login_required
def add_voucher(request):
    voucher_type = request.GET.get("type", "Payment")
    ledger_accounts = LedgerAccount.objects.select_related("group").order_by("group__nature", "name")
    farmers = Farmer.objects.order_by("name")
    traders = Trader.objects.order_by("name")

    if request.method == "POST":
        voucher_type = request.POST.get("voucher_type", "Payment")
        voucher_no = request.POST.get("voucher_no", "").strip() or _next_voucher_no(voucher_type)
        date_str = request.POST.get("date", "")
        narration = request.POST.get("narration", "")
        bikri_bill_no = request.POST.get("bikri_bill_no", "").strip()

        if not date_str:
            messages.error(request, "Date is required.")
        else:
            lines_data, line_err = _parse_voucher_lines_from_post(request)
            if line_err:
                messages.error(request, line_err)
            else:
                with transaction.atomic():
                    voucher = Voucher.objects.create(
                        voucher_type=voucher_type,
                        voucher_no=voucher_no,
                        date=date_str,
                        narration=narration,
                        bikri_bill_no=bikri_bill_no or None,
                    )
                    for (lid, etype, amt, lnar) in lines_data:
                        VoucherLine.objects.create(
                            voucher=voucher,
                            ledger_id=lid,
                            entry_type=etype,
                            amount=amt,
                            narration=lnar or None,
                        )
                messages.success(request, f"Voucher {voucher.voucher_no} saved successfully.")
                return redirect("voucher_list")

    suggested_no = _next_voucher_no(voucher_type)
    return render(request, "accounts/voucher_entry.html", {
        "voucher_type": voucher_type,
        "voucher_types": Voucher.VOUCHER_TYPES,
        "ledger_accounts": ledger_accounts,
        "farmers": farmers,
        "traders": traders,
        "today": date.today().strftime("%Y-%m-%d"),
        "suggested_no": suggested_no,
    })


@login_required
def edit_voucher(request, voucher_id):
    voucher = get_object_or_404(Voucher, id=voucher_id)

    ledger_accounts = LedgerAccount.objects.select_related("group").order_by("group__nature", "name")
    farmers = Farmer.objects.order_by("name")
    traders = Trader.objects.order_by("name")

    if request.method == "POST":
        voucher_type = request.POST.get("voucher_type", voucher.voucher_type)
        voucher_no = request.POST.get("voucher_no", "").strip() or voucher.voucher_no
        date_str = request.POST.get("date", "")
        narration = request.POST.get("narration", "")
        bikri_bill_no = request.POST.get("bikri_bill_no", "").strip()

        if not date_str:
            messages.error(request, "Date is required.")
        else:
            lines_data, line_err = _parse_voucher_lines_from_post(request)
            if line_err:
                messages.error(request, line_err)
            else:
                with transaction.atomic():
                    voucher.voucher_type = voucher_type
                    voucher.voucher_no = voucher_no
                    voucher.date = date_str
                    voucher.narration = narration
                    voucher.bikri_bill_no = bikri_bill_no or None
                    voucher.save()
                    voucher.lines.all().delete()
                    for (lid, etype, amt, lnar) in lines_data:
                        VoucherLine.objects.create(
                            voucher=voucher,
                            ledger_id=lid,
                            entry_type=etype,
                            amount=amt,
                            narration=lnar or None,
                        )
                messages.success(
                    request,
                    f"Voucher {voucher.voucher_no} updated. Account statements reflect the changes.",
                )
                return redirect("view_voucher", voucher_id=voucher.id)

    existing_lines = []
    for line in voucher.lines.select_related("ledger__farmer", "ledger__trader").all():
        existing_lines.append({
            "ledger_id": _ledger_select_value(line.ledger),
            "entry_type": line.entry_type,
            "amount": str(line.amount),
            "narration": line.narration or "",
        })

    return render(request, "accounts/voucher_entry.html", {
        "voucher": voucher,
        "is_edit": True,
        "voucher_type": voucher.voucher_type,
        "voucher_types": Voucher.VOUCHER_TYPES,
        "ledger_accounts": ledger_accounts,
        "farmers": farmers,
        "traders": traders,
        "today": voucher.date.strftime("%Y-%m-%d"),
        "suggested_no": voucher.voucher_no,
        "existing_lines_json": json.dumps(existing_lines),
        "bikri_bill_no": voucher.bikri_bill_no or "",
        "is_auto_voucher": voucher.is_auto,
    })


@login_required
def view_voucher(request, voucher_id):
    voucher = get_object_or_404(Voucher, id=voucher_id)
    lines = voucher.lines.select_related("ledger__group").all()
    view_mode = request.GET.get("view", "voucher")
    if view_mode not in ("voucher", "receipt"):
        view_mode = "voucher"
    if view_mode == "receipt" and not voucher.ref_bikri:
        view_mode = "voucher"

    # Compute patti_no for auto Vikri vouchers
    patti_no = None
    if voucher.ref_bikri:
        if voucher.bikri_bill_no:
            patti_no = voucher.bikri_bill_no
        else:
            from django.db.models import Min as _Min
            bikri = voucher.ref_bikri
            farmer_order = (
                Bikri.objects.filter(date=bikri.date, is_cancelled=False)
                .values("avak__farmer_id")
                .annotate(first_id=_Min("id"))
                .order_by("first_id")
            )
            patti_no = next(
                (i + 1 for i, f in enumerate(farmer_order) if f["avak__farmer_id"] == bikri.avak.farmer_id),
                1,
            )

    return render(request, "accounts/view_voucher.html", {
        "voucher": voucher,
        "lines": lines,
        "patti_no": patti_no,
        "view_mode": view_mode,
    })


@login_required
def delete_voucher(request, voucher_id):
    voucher = get_object_or_404(Voucher, id=voucher_id)
    if voucher.is_auto:
        messages.error(request, "Auto-generated vouchers cannot be deleted manually.")
        return redirect("voucher_list")
    voucher.delete()
    messages.success(request, "Voucher deleted.")
    return redirect("voucher_list")


# ── API: Ledger accounts for Select2 ──────────────────────────────────────────

@login_required
def api_get_ledger_accounts(request):
    q = request.GET.get("q", "").strip()
    qs = LedgerAccount.objects.select_related("group")
    if q:
        qs = qs.filter(name__icontains=q)
    results = [
        {"id": la.id, "text": f"{la.name}  ({la.group.name})"}
        for la in qs[:50]
    ]
    return JsonResponse({"results": results})


# ── AUTO-LEDGER: create voucher from Vikri Patti (Bikri) ─────────────────────

_LEDGER_NAME_ALIASES = {
    "Commission Account": ("Commission / Dalali Income",),
    "Hamali": ("Hamali Income",),
    "Audit Fee": ("Audit Fees",),
    "Od Intrest": ("OD Interest", "Od Interest"),
}


def _get_or_create_ledger(name, group_name):
    """Get or create a ledger account by name (for auto-vouchers)."""
    try:
        return LedgerAccount.objects.get(name=name)
    except LedgerAccount.DoesNotExist:
        for alias in _LEDGER_NAME_ALIASES.get(name, ()):
            try:
                legacy = LedgerAccount.objects.get(name=alias)
                LedgerAccount.objects.filter(pk=legacy.pk).update(name=name)
                legacy.name = name
                return legacy
            except LedgerAccount.DoesNotExist:
                pass
        try:
            group = LedgerGroup.objects.get(name=group_name)
        except LedgerGroup.DoesNotExist:
            ensure_default_ledgers()
            try:
                group = LedgerGroup.objects.get(name=group_name)
            except LedgerGroup.DoesNotExist:
                return None
        return LedgerAccount.objects.create(name=name, group=group, is_system=True)


def _build_bikri_voucher(bikri):
    """Auto-create a combined Journal voucher for ALL Bikri lots of the same farmer on the same date.
    Credits farmer with the combined net payable (matching view_bikri ನಿವ್ವಳ ಅಮೌಂಟ್).
    Debits 'Bazar Sales Receivable' (clearing account) — cleared later when trader bill is saved.
    Returns existing voucher if already created, or None if ledger groups are missing."""

    # All lots for this farmer on this date (combined bill)
    all_group = list(
        Bikri.objects.filter(
            date=bikri.date,
            avak__farmer=bikri.avak.farmer,
            is_cancelled=False,
        ).select_related("avak", "buyer").order_by("id")
    )

    # Return existing voucher if already created for ANY lot in this group
    existing = Voucher.objects.filter(ref_bikri__in=all_group, is_auto=True).first()
    if existing:
        return existing

    # Resolve patti_no (same logic as view_bikri)
    patti_no = (bikri.bill_no or "").strip()
    if not patti_no:
        from django.db.models import Min as _Min
        farmer_order = (
            Bikri.objects.filter(date=bikri.date, is_cancelled=False)
            .values("avak__farmer_id")
            .annotate(first_id=_Min("id"))
            .order_by("first_id")
        )
        patti_no = str(next(
            (i + 1 for i, f in enumerate(farmer_order) if f["avak__farmer_id"] == bikri.avak.farmer_id),
            1,
        ))

    # ── Farmer ledger account ─────────────────────────────────────────────────
    try:
        farmer_ledger_acc = bikri.avak.farmer.ledger_account
    except LedgerAccount.DoesNotExist:
        farmer_ledger_acc = _get_or_create_ledger(
            f"Farmer: {bikri.avak.farmer.name}", "Sundry Creditors"
        )
        if farmer_ledger_acc and not farmer_ledger_acc.farmer_id:
            LedgerAccount.objects.filter(pk=farmer_ledger_acc.pk).update(farmer=bikri.avak.farmer)

    # ── Clearing account and farmer deductions ledger ─────────────────────────
    bazar_sales_receivable = _get_or_create_ledger("Bazar Sales Receivable", "Sundry Debtors")
    deductions_ledger      = _get_or_create_ledger("Farmer Deductions Income", "Direct Incomes")

    if any(l is None for l in [farmer_ledger_acc, bazar_sales_receivable, deductions_ledger]):
        return None

    # ── Combined amounts across all lots ──────────────────────────────────────
    combined_net_payable = _calc_group_net_payable(all_group, bikri.date)
    combined_gross       = sum(_to_decimal(b.amount) for b in all_group)
    combined_packing     = sum(_to_decimal(b.packing) for b in all_group)

    # Farmer-side deductions (rakham, rent, farmer hamali, advance etc.) → APMC income
    farmer_deductions = _quantize_money(combined_gross - combined_net_payable)

    # ── Build voucher lines ───────────────────────────────────────────────────
    # Dr: Bazar Sales Receivable (goods + trader packing — cleared on ಖರೀದಿ ಪಟ್ಟಿ)
    vlines = []
    bazar_dr = combined_gross + combined_packing
    if bazar_dr > 0:
        vlines.append((bazar_sales_receivable, "Dr", bazar_dr,
                        "ಬಜಾರ್ ಮಾರಾಟ ಮೊತ್ತ (goods sold, pending trader billing)"))

    if combined_packing > 0:
        packing_ledger = _get_or_create_ledger("Packing Income", "Direct Incomes")
        if packing_ledger:
            vlines.append((packing_ledger, "Cr", combined_packing,
                            f"Packing Income – ಪ.ಸಂ {patti_no}"))

    # Cr: farmer gets combined net payable (matches ನಿವ್ವಳ ಅಮೌಂಟ್ in view_bikri)
    if combined_net_payable > 0:
        vlines.append((farmer_ledger_acc, "Cr", combined_net_payable,
                        f"ನಿವ್ವಳ ಮೊತ್ತ – ಪ.ಸಂ {patti_no}"))

    # Cr: farmer deductions (rakham, rent, farmer hamali, advance → APMC income)
    if farmer_deductions > 0:
        vlines.append((deductions_ledger, "Cr", farmer_deductions,
                        "Farmer deductions (ರಖಂ, ಬಾಡಿಗೆ, ಹಮಾಲಿ, etc.)"))
    elif farmer_deductions < 0:
        vlines.append((deductions_ledger, "Dr", abs(farmer_deductions),
                        "Farmer packing excess over deductions"))

    # Round-off balancing (should not be needed with clean formula, but safety net)
    total_dr_amt = sum(amt for (_, et, amt, _) in vlines if et == "Dr")
    total_cr_amt = sum(amt for (_, et, amt, _) in vlines if et == "Cr")
    diff = total_dr_amt - total_cr_amt
    if diff != 0 and abs(diff) < Decimal("10"):
        round_off = _get_or_create_ledger("Round Off", "Expenses")
        if round_off:
            if diff > 0:
                vlines.append((round_off, "Cr", diff,      "Round off"))
            else:
                vlines.append((round_off, "Dr", abs(diff), "Round off"))

    with transaction.atomic():
        voucher = Voucher.objects.create(
            voucher_type="Journal",
            voucher_no=_next_voucher_no("Journal"),
            date=bikri.date,
            narration=f"Auto: ವಿಕ್ರಿ ಪಟ್ಟಿ – ಪ.ಸಂ {patti_no} – {bikri.avak.farmer.name}",
            bikri_bill_no=str(patti_no),
            ref_bikri=bikri,
            is_auto=True,
        )
        for (ledger_obj, etype, amt, nar) in vlines:
            VoucherLine.objects.create(
                voucher=voucher, ledger=ledger_obj,
                entry_type=etype, amount=amt, narration=nar,
            )
    return voucher


def _build_trader_bill_voucher(trader_bill):
    """Create or recreate a Journal voucher for a saved TraderBill.
    Dr: Trader Account (grand_total)
    Cr: Bazar Sales Receivable (total_amount) + income accounts (charges)
    Uses trader bill invoice_no — NOT the vikri bill number.
    Returns voucher, or None if ledger groups are missing."""

    # Get or create trader's ledger account
    try:
        trader_ledger_acc = trader_bill.buyer.ledger_account
    except LedgerAccount.DoesNotExist:
        trader_ledger_acc = _get_or_create_ledger(
            f"Trader: {trader_bill.buyer.name}", "Sundry Debtors"
        )
        if trader_ledger_acc and not trader_ledger_acc.trader_id:
            LedgerAccount.objects.filter(pk=trader_ledger_acc.pk).update(trader=trader_bill.buyer)

    bazar_sales_receivable = _get_or_create_ledger("Bazar Sales Receivable", "Sundry Debtors")
    commission_ledger      = _get_or_create_ledger("Commission Account", "Direct Incomes")
    cess_ledger            = _get_or_create_ledger("Cess Income", "Provision(Payable)")
    output_sgst_ledger     = _get_or_create_ledger("Output SGST", "Provision(Payable)")
    output_cgst_ledger     = _get_or_create_ledger("Output CGST", "Provision(Payable)")

    if any(l is None for l in [trader_ledger_acc, bazar_sales_receivable,
                                commission_ledger, cess_ledger,
                                output_sgst_ledger, output_cgst_ledger]):
        return None

    bill_total   = _to_decimal(trader_bill.total_amount)   # goods value only
    grand_total  = _to_decimal(trader_bill.grand_total)    # what trader owes APMC
    commission   = _to_decimal(trader_bill.commission)     # dalali % only → Direct Incomes
    hamali       = _to_decimal(trader_bill.hamali)
    packing      = _to_decimal(trader_bill.packing)
    weighman_fee = _to_decimal(trader_bill.weighman_fee)
    cess         = _to_decimal(trader_bill.cess)
    bazar_clear  = bill_total + hamali + packing + weighman_fee
    gst          = _to_decimal(trader_bill.gst)
    round_off    = _to_decimal(trader_bill.round_off)

    vlines = []
    # Dr: Trader — full grand_total (matches ಖರೀದಿ ಪಟ್ಟಿ view / invoice amount)
    if grand_total > 0:
        vlines.append((trader_ledger_acc, "Dr", grand_total,
                        f"ಖರೀದಿ ಪಟ್ಟಿ #{trader_bill.invoice_no} – {trader_bill.buyer.name}"))

    # Cr: Clear Bazar Sales Receivable (goods + hamali + packing + weighman)
    if bazar_clear > 0:
        vlines.append((bazar_sales_receivable, "Cr", bazar_clear,
                        f"ಸರಕು + ಹಮಾಲಿ/ಪ್ಯಾಕಿಂಗ್ – ಖರೀದಿ ಪಟ್ಟಿ #{trader_bill.invoice_no}"))

    # Cr: Dalali / commission only → Direct Incomes
    if commission > 0:
        vlines.append((commission_ledger, "Cr", commission, "Commission Account"))
    if cess > 0:
        vlines.append((cess_ledger, "Cr", cess, "Cess"))
    if gst > 0:
        cgst_amt = _quantize_money(gst / 2)
        sgst_amt = gst - cgst_amt
        bill_ref = f"Bill #{trader_bill.invoice_no}"
        if sgst_amt > 0:
            vlines.append((output_sgst_ledger, "Cr", sgst_amt,
                            f"Output SGST @ 2.5% – {bill_ref}"))
        if cgst_amt > 0:
            vlines.append((output_cgst_ledger, "Cr", cgst_amt,
                            f"Output CGST @ 2.5% – {bill_ref}"))

    # Round off balancing
    if round_off != 0:
        round_off_ledger = _get_or_create_ledger("Round Off", "Expenses")
        if round_off_ledger:
            if round_off > 0:
                vlines.append((round_off_ledger, "Cr", round_off,  "Round off"))
            else:
                vlines.append((round_off_ledger, "Dr", abs(round_off), "Round off"))

    # Residual balance check
    total_dr_amt = sum(amt for (_, et, amt, _) in vlines if et == "Dr")
    total_cr_amt = sum(amt for (_, et, amt, _) in vlines if et == "Cr")
    diff = total_dr_amt - total_cr_amt
    if diff != 0 and abs(diff) < Decimal("10"):
        residual_ledger = _get_or_create_ledger("Round Off", "Expenses")
        if residual_ledger:
            if diff > 0:
                vlines.append((residual_ledger, "Cr", diff,      "Balance adjust"))
            else:
                vlines.append((residual_ledger, "Dr", abs(diff), "Balance adjust"))

    with transaction.atomic():
        voucher = Voucher.objects.create(
            voucher_type="Journal",
            voucher_no=_next_voucher_no("Journal"),
            date=trader_bill.date,
            narration=(
                f"Auto: ಖರೀದಿ ಪಟ್ಟಿ #{trader_bill.invoice_no} – "
                f"{trader_bill.buyer.name} – {trader_bill.date}"
            ),
            bikri_bill_no=str(trader_bill.invoice_no),
            ref_trader_bill=trader_bill,
            is_auto=True,
        )
        for (ledger_obj, etype, amt, nar) in vlines:
            VoucherLine.objects.create(
                voucher=voucher, ledger=ledger_obj,
                entry_type=etype, amount=amt, narration=nar,
            )
    return voucher


@login_required
def create_voucher_from_bikri(request, bikri_id):
    """Create / recreate a Journal voucher from a Vikri Patti (Bikri) record.
    Always deletes and recreates the auto-voucher so new lots added after the first
    creation are included in the combined calculation."""
    bikri = get_object_or_404(Bikri, id=bikri_id)

    # Delete any existing auto-voucher for this farmer+date group before recreating
    all_group_qs = Bikri.objects.filter(
        date=bikri.date,
        avak__farmer=bikri.avak.farmer,
        is_cancelled=False,
    )
    Voucher.objects.filter(ref_bikri__in=all_group_qs, is_auto=True).delete()

    voucher = _build_bikri_voucher(bikri)
    if voucher is None:
        messages.error(request, "Missing ledger groups. Please set up Ledger Master first.")
        return redirect("ledger_master")

    messages.success(request, f"Journal voucher {voucher.voucher_no} created for Vikri Patti.")
    return redirect("view_voucher", voucher_id=voucher.id)


@login_required
def create_voucher_from_trader_bill(request, bill_id):
    """Create / recreate a Journal voucher from a saved ಖರೀದಿ ಪಟ್ಟಿ (TraderBill)."""
    from accounts.models import TraderBill
    bill = get_object_or_404(TraderBill, id=bill_id)

    # Delete existing auto-voucher before recreating (so it reflects latest bill totals)
    existing_qs = Voucher.objects.filter(ref_trader_bill=bill, is_auto=True)
    if existing_qs.exists() and request.method != "POST":
        # If accessed via GET, just redirect to the existing voucher
        return redirect("view_voucher", voucher_id=existing_qs.first().id)
    existing_qs.delete()

    voucher = _build_trader_bill_voucher(bill)
    if voucher is None:
        messages.error(request, "Missing ledger groups. Please set up Ledger Master first.")
        return redirect("ledger_master")

    messages.success(request, f"Journal voucher {voucher.voucher_no} created for ಖರೀದಿ ಪಟ್ಟಿ #{bill.invoice_no}.")
    return redirect("view_voucher", voucher_id=voucher.id)


# ── TRIAL BALANCE ─────────────────────────────────────────────────────────────

@login_required
def trial_balance(request):
    """Trial Balance grouped by Tally-style heads as on selected date."""
    date_param = request.GET.get("as_on_date", "")
    if date_param:
        try:
            as_on = date.fromisoformat(date_param)
        except ValueError:
            as_on = date.today()
    else:
        as_on = date.today()

    ledgers_qs = LedgerAccount.objects.select_related(
        "group", "farmer", "trader"
    ).order_by("name")

    cash_la = LedgerAccount.objects.filter(name=CASH_IN_HAND_LEDGER_NAME).first()

    head_buckets = {head: [] for head in TRIAL_BALANCE_HEAD_ORDER}
    grand_dr = Decimal("0")
    grand_cr = Decimal("0")
    net_grand_dr = Decimal("0")
    net_grand_cr = Decimal("0")

    for ledger in ledgers_qs:
        if cash_la and ledger.pk == cash_la.pk:
            continue
        tb_head = _trial_balance_head_for_ledger(ledger)
        if not tb_head:
            continue
        skip_bank_ft = tb_head in ("Bank Accounts", "Bank OD A/C")
        dr_amt, cr_amt = _calc_ledger_trial_display_dr_cr(
            ledger, as_on, skip_ft_pay=skip_bank_ft
        )
        net_dr, net_cr = _calc_ledger_closing_as_on(ledger, as_on)
        if dr_amt == 0 and cr_amt == 0 and tb_head not in TRIAL_BALANCE_SHOW_ALL_LEDGER_HEADS:
            continue
        head_buckets.setdefault(tb_head, []).append({
            "name": ledger.name,
            "place": _ledger_place_for_trial(ledger),
            "debit": dr_amt,
            "credit": cr_amt,
            "debit_fmt": _format_indian_amount(dr_amt),
            "credit_fmt": _format_indian_amount(cr_amt),
        })
        net_grand_dr += net_dr
        net_grand_cr += net_cr

    report_groups = []
    serial = 0
    for head in sorted(head_buckets.keys(), key=_trial_balance_head_sort_key):
        rows = sorted(head_buckets[head], key=lambda r: r["name"].lower())
        if not rows:
            continue
        for row in rows:
            serial += 1
            row["serial"] = serial
        group_dr = sum(r["debit"] for r in rows)
        group_cr = sum(r["credit"] for r in rows)
        report_groups.append({
            "head": head,
            "rows": rows,
            "subtotal_dr": group_dr,
            "subtotal_cr": group_cr,
            "subtotal_dr_fmt": _format_indian_amount(group_dr),
            "subtotal_cr_fmt": _format_indian_amount(group_cr),
        })
        grand_dr += group_dr
        grand_cr += group_cr

    cash_row = None
    if cash_la:
        c_dr, c_cr = _calc_ledger_trial_display_dr_cr(cash_la, as_on, skip_ft_pay=True)
        if c_dr or c_cr:
            cash_row = {
                "name": "Cash In Hand",
                "debit_fmt": _format_indian_amount(c_dr),
                "credit_fmt": _format_indian_amount(c_cr),
                "debit": c_dr,
                "credit": c_cr,
            }

    return render(request, "accounts/reports/trial_balance.html", {
        "as_on_date": as_on.strftime("%Y-%m-%d"),
        "as_on_display": as_on.strftime("%d-%m-%Y"),
        "report_groups": report_groups,
        "grand_dr": grand_dr,
        "grand_cr": grand_cr,
        "grand_dr_fmt": _format_indian_amount(grand_dr),
        "grand_cr_fmt": _format_indian_amount(grand_cr),
        "cash_row": cash_row,
        "is_balanced": net_grand_dr == net_grand_cr,
        "system_name": "MSBC-2025-26",
    })


@login_required
def profit_and_loss(request):
    """Trading Account + Profit & Loss for a date range (T-format)."""
    from django.db.models import Sum, Count

    from_date_str = request.GET.get("from_date", "")
    to_date_str = request.GET.get("to_date", "")

    if to_date_str:
        try:
            to_date = date.fromisoformat(to_date_str)
        except ValueError:
            to_date = date.today()
    else:
        to_date = date.today()

    if from_date_str:
        try:
            from_date = date.fromisoformat(from_date_str)
        except ValueError:
            from_date = to_date.replace(month=4, day=1) if to_date.month >= 4 else to_date.replace(year=to_date.year - 1, month=4, day=1)
    else:
        from_date = to_date.replace(month=4, day=1) if to_date.month >= 4 else to_date.replace(year=to_date.year - 1, month=4, day=1)

    if from_date > to_date:
        from_date, to_date = to_date, from_date

    bikri_qs = Bikri.objects.filter(
        is_cancelled=False, date__gte=from_date, date__lte=to_date
    )
    purchase_amt = _quantize_money(
        bikri_qs.aggregate(t=Sum("amount"))["t"] or Decimal("0")
    )
    purchase_weight = bikri_qs.aggregate(t=Sum("total_weight"))["t"] or Decimal("0")
    purchase_weight = _quantize_money(purchase_weight)

    sales_amt = _quantize_money(
        TraderBill.objects.filter(date__gte=from_date, date__lte=to_date)
        .aggregate(t=Sum("total_amount"))["t"] or Decimal("0")
    )
    sales_weight = purchase_weight

    variety_row = (
        Avak.objects.filter(
            bikri_entries__date__gte=from_date,
            bikri_entries__date__lte=to_date,
            bikri_entries__is_cancelled=False,
        )
        .exclude(variety="")
        .exclude(variety__isnull=True)
        .values("variety")
        .annotate(c=Count("id"))
        .order_by("-c")
        .first()
    )
    commodity = (variety_row or {}).get("variety") or "Dry Chillies"

    gross_profit = sales_amt - purchase_amt
    gross_loss = Decimal("0")
    if gross_profit < 0:
        gross_loss = abs(gross_profit)
        gross_profit = Decimal("0")

    trading_debit = [
        {"label": "Opening Balance", "qty": "", "amount": "", "amount_fmt": ""},
        {"label": "Purchase", "qty": f"{purchase_weight:.2f}", "amount": purchase_amt, "amount_fmt": _fmt_pl(purchase_amt)},
        {"label": "Credit Note", "qty": "", "amount": "", "amount_fmt": ""},
        {"label": "Sale Return", "qty": "", "amount": "", "amount_fmt": ""},
        {"label": "Received from Production", "qty": "", "amount": "", "amount_fmt": ""},
    ]
    if gross_loss > 0:
        trading_debit.append({
            "label": "Gross Loss",
            "qty": "",
            "amount": gross_loss,
            "amount_fmt": _fmt_pl(gross_loss),
        })

    trading_credit = [
        {"label": "Sales", "qty": f"{sales_weight:.2f}", "amount": sales_amt, "amount_fmt": _fmt_pl(sales_amt)},
        {"label": "Debit Note", "qty": "", "amount": "", "amount_fmt": ""},
        {"label": "Purchase Return", "qty": "", "amount": "", "amount_fmt": ""},
        {"label": "Sent For Production", "qty": "", "amount": "", "amount_fmt": ""},
        {"label": "Weight Shortage", "qty": "", "amount": "", "amount_fmt": ""},
        {"label": "Closing Weight", "qty": "", "amount": "", "amount_fmt": ""},
    ]
    if gross_profit > 0:
        trading_credit.append({
            "label": "Gross Profit",
            "qty": "",
            "amount": gross_profit,
            "amount_fmt": _fmt_pl(gross_profit),
        })

    trading_dr_total = purchase_amt + gross_loss
    trading_cr_total = sales_amt + gross_profit

    max_tr = max(len(trading_debit), len(trading_credit))
    trading_rows = []
    for i in range(max_tr):
        d = trading_debit[i] if i < len(trading_debit) else None
        c = trading_credit[i] if i < len(trading_credit) else None
        trading_rows.append({"debit": d, "credit": c})

    income_ledgers = LedgerAccount.objects.filter(
        group__nature="Income"
    ).select_related("group").order_by("group__name", "name")

    pl_income = []
    if gross_profit > 0:
        pl_income.append({
            "name": "Gross Profit",
            "amount": gross_profit,
            "amount_fmt": _fmt_pl(gross_profit),
        })
    total_indirect_income = gross_profit
    for la in income_ledgers:
        if la.name in PL_EXCLUDE_INCOME_LEDGER_NAMES:
            continue
        amt = _ledger_pl_amount(la, from_date, to_date)
        if amt <= 0:
            continue
        pl_income.append({
            "name": la.name,
            "amount": amt,
            "amount_fmt": _fmt_pl(amt),
        })
        total_indirect_income += amt

    expense_ledgers = LedgerAccount.objects.filter(
        group__nature="Expenses"
    ).select_related("group").order_by("group__name", "name")

    pl_expenses = []
    total_indirect_expenses = Decimal("0")
    for la in expense_ledgers:
        amt = _ledger_pl_amount(la, from_date, to_date)
        if amt <= 0:
            continue
        pl_expenses.append({
            "name": la.name,
            "amount": amt,
            "amount_fmt": _fmt_pl(amt),
        })
        total_indirect_expenses += amt

    max_pl = max(len(pl_expenses), len(pl_income), 1)
    pl_rows = []
    for i in range(max_pl):
        exp = pl_expenses[i] if i < len(pl_expenses) else None
        inc = pl_income[i] if i < len(pl_income) else None
        pl_rows.append({"expense": exp, "income": inc})

    net_profit = total_indirect_income - total_indirect_expenses
    net_loss = Decimal("0")
    if net_profit < 0:
        net_loss = abs(net_profit)
        net_profit = Decimal("0")

    period_label = f"{from_date.strftime('%d-%m-%Y')} to {to_date.strftime('%d-%m-%Y')}"

    commodity_rowspan = (
        len(trading_rows) + 1 + 1 + len(pl_rows) + 1 + (1 if net_profit or net_loss else 0)
    )

    return render(request, "accounts/reports/profit_and_loss.html", {
        "from_date": from_date.strftime("%Y-%m-%d"),
        "to_date": to_date.strftime("%Y-%m-%d"),
        "period_label": period_label,
        "to_display": to_date.strftime("%d-%m-%Y"),
        "commodity": commodity,
        "commodity_rowspan": commodity_rowspan,
        "trading_rows": trading_rows,
        "trading_row_count": max_tr,
        "trading_debit": trading_debit,
        "trading_credit": trading_credit,
        "trading_dr_total_fmt": _fmt_pl(trading_dr_total),
        "trading_cr_total_fmt": _fmt_pl(trading_cr_total),
        "trading_dr_qty_fmt": f"{purchase_weight:.2f}" if purchase_weight else "",
        "trading_cr_qty_fmt": f"{sales_weight:.2f}" if sales_weight else "",
        "gross_profit": gross_profit,
        "gross_profit_fmt": _fmt_pl(gross_profit),
        "pl_income": pl_income,
        "pl_expenses": pl_expenses,
        "pl_rows": pl_rows,
        "total_indirect_income": total_indirect_income,
        "total_indirect_income_fmt": _fmt_pl(total_indirect_income),
        "total_indirect_expenses": total_indirect_expenses,
        "total_indirect_expenses_fmt": _fmt_pl(total_indirect_expenses),
        "net_profit": net_profit,
        "net_profit_fmt": _fmt_pl(net_profit),
        "net_loss": net_loss,
        "net_loss_fmt": _fmt_pl(net_loss),
    })


# ── LEDGER BOOK (Account Statement) ──────────────────────────────────────────

def _get_voucher_bill_no(voucher):
    """Return bill / patti / invoice number linked to a voucher."""
    if not voucher:
        return ""
    raw = (voucher.bikri_bill_no or "").strip()
    if raw:
        return raw
    ref_trader_bill = getattr(voucher, "ref_trader_bill", None)
    if getattr(voucher, "ref_trader_bill_id", None) and ref_trader_bill:
        return str(ref_trader_bill.invoice_no)
    ref_bikri = getattr(voucher, "ref_bikri", None)
    if getattr(voucher, "ref_bikri_id", None) and ref_bikri:
        bill_no = (ref_bikri.bill_no or "").strip()
        if bill_no:
            return bill_no
    return ""


def _ft_bill_no(ft):
    """Return bill reference from a financial transaction."""
    if not ft:
        return ""
    return (ft.bikri_bill_no or "").strip()


def _get_bill_info(voucher):
    """Return extra lot/trader context for a voucher (beyond bill no)."""
    bikri = voucher.ref_bikri
    if not bikri:
        return ""
    parts = []
    try:
        parts.append(f"Lot: {bikri.avak.lot_number}")
    except Exception:
        pass
    try:
        parts.append(f"Inv: {bikri.traderbillitem.bill.invoice_no}")
    except Exception:
        pass
    return " | ".join(parts)


FY_MONTHS = (
    (4, "April"),
    (5, "May"),
    (6, "June"),
    (7, "July"),
    (8, "August"),
    (9, "September"),
    (10, "October"),
    (11, "November"),
    (12, "December"),
    (1, "January"),
    (2, "February"),
    (3, "March"),
)


def _current_financial_year_start(today=None):
    today = today or date.today()
    return today.year if today.month >= 4 else today.year - 1


def _fy_month_bounds(fy_start, month_num):
    year = fy_start if month_num >= 4 else fy_start + 1
    last_day = calendar.monthrange(year, month_num)[1]
    return date(year, month_num, 1), date(year, month_num, last_day)


def _signed_opening_balance(opening_balance, balance_type):
    """Positive = net credit balance, negative = net debit balance."""
    ob = _to_decimal(opening_balance, "0")
    return ob if balance_type == "Cr" else -ob


def _build_monthwise_from_dr_cr_entries(opening_balance, balance_type, entries, fy_start):
    """Build April–March monthwise rows from {date, dr_amount, cr_amount} entries."""
    fy_begin = date(fy_start, 4, 1)
    signed = _signed_opening_balance(opening_balance, balance_type)
    for entry in entries:
        if entry["date"] < fy_begin:
            signed += entry["cr_amount"] - entry["dr_amount"]

    rows = []
    for idx, (month_num, month_name) in enumerate(FY_MONTHS, start=1):
        month_start, month_end = _fy_month_bounds(fy_start, month_num)
        opening = signed
        month_dr = Decimal("0")
        month_cr = Decimal("0")
        for entry in entries:
            if month_start <= entry["date"] <= month_end:
                month_dr += entry["dr_amount"]
                month_cr += entry["cr_amount"]
        closing = opening + month_cr - month_dr
        rows.append({
            "sno": idx,
            "month": month_name,
            "opening": opening,
            "credit": month_cr,
            "debit": month_dr,
            "closing": closing,
            "end_date": month_end.strftime("%Y-%m-%d"),
        })
        signed = closing
    return rows


def _build_monthwise_for_ledger(ledger, fy_start):
    fy_begin = date(fy_start, 4, 1)
    signed = _signed_opening_balance(ledger.opening_balance, ledger.balance_type)
    prior = VoucherLine.objects.filter(
        ledger=ledger, voucher__date__lt=fy_begin
    ).aggregate(
        dr=Sum("amount", filter=Q(entry_type="Dr")),
        cr=Sum("amount", filter=Q(entry_type="Cr")),
    )
    signed += (_to_decimal(prior["cr"], "0") - _to_decimal(prior["dr"], "0"))

    rows = []
    for idx, (month_num, month_name) in enumerate(FY_MONTHS, start=1):
        month_start, month_end = _fy_month_bounds(fy_start, month_num)
        opening = signed
        totals = VoucherLine.objects.filter(
            ledger=ledger,
            voucher__date__gte=month_start,
            voucher__date__lte=month_end,
        ).aggregate(
            dr=Sum("amount", filter=Q(entry_type="Dr")),
            cr=Sum("amount", filter=Q(entry_type="Cr")),
        )
        month_dr = _to_decimal(totals["dr"], "0")
        month_cr = _to_decimal(totals["cr"], "0")
        closing = opening + month_cr - month_dr
        rows.append({
            "sno": idx,
            "month": month_name,
            "opening": opening,
            "credit": month_cr,
            "debit": month_dr,
            "closing": closing,
            "end_date": month_end.strftime("%Y-%m-%d"),
        })
        signed = closing
    return rows


def _collect_farmer_trader_entries(entity_type, selected_farmer, selected_trader):
    """All voucher + financial transaction rows for monthwise (no date filter)."""
    entries = []
    if entity_type == "farmer":
        opening_balance = selected_farmer.opening_balance
        opening_balance_type = selected_farmer.balance_type
        try:
            linked_la = selected_farmer.ledger_account
            for line in VoucherLine.objects.filter(ledger=linked_la).select_related("voucher"):
                entries.append({
                    "date": line.voucher.date,
                    "dr_amount": line.amount if line.entry_type == "Dr" else Decimal("0"),
                    "cr_amount": line.amount if line.entry_type == "Cr" else Decimal("0"),
                })
        except LedgerAccount.DoesNotExist:
            pass
        for ft in FinancialTransaction.objects.filter(farmer=selected_farmer):
            entries.append({
                "date": ft.date,
                "dr_amount": ft.amount if ft.transaction_type == "Debit" else Decimal("0"),
                "cr_amount": ft.amount if ft.transaction_type == "Credit" else Decimal("0"),
            })
    elif entity_type == "trader":
        opening_balance = selected_trader.opening_balance
        opening_balance_type = selected_trader.balance_type
        try:
            linked_la = selected_trader.ledger_account
            for line in VoucherLine.objects.filter(ledger=linked_la).select_related("voucher"):
                entries.append({
                    "date": line.voucher.date,
                    "dr_amount": line.amount if line.entry_type == "Dr" else Decimal("0"),
                    "cr_amount": line.amount if line.entry_type == "Cr" else Decimal("0"),
                })
        except LedgerAccount.DoesNotExist:
            pass
        for ft in FinancialTransaction.objects.filter(trader=selected_trader):
            entries.append({
                "date": ft.date,
                "dr_amount": ft.amount if ft.transaction_type == "Debit" else Decimal("0"),
                "cr_amount": ft.amount if ft.transaction_type == "Credit" else Decimal("0"),
            })
    else:
        return [], Decimal("0"), "Dr"
    return entries, opening_balance, opening_balance_type


def _build_monthwise_for_selection(entity_type, selected_ledger, selected_farmer, selected_trader, fy_start):
    """Return (account_display_name, monthwise_rows) for the selected account."""
    if entity_type == "ledger" and selected_ledger:
        return selected_ledger.name, _build_monthwise_for_ledger(selected_ledger, fy_start)
    if entity_type in ("farmer", "trader"):
        party_entries, ob, ob_type = _collect_farmer_trader_entries(
            entity_type, selected_farmer, selected_trader
        )
        name = selected_farmer.name if entity_type == "farmer" else selected_trader.name
        return name, _build_monthwise_from_dr_cr_entries(ob, ob_type, party_entries, fy_start)
    return "", []


@login_required
def ledger_book(request):
    """Full ledger book – choose an account (incl. farmers/traders) and date range."""
    ledger_accounts = LedgerAccount.objects.select_related("group").order_by("group__nature", "name")
    farmers = Farmer.objects.order_by("name")
    traders = Trader.objects.order_by("name")

    ledger_id = request.GET.get("ledger_id", "")
    from_date = request.GET.get("from_date", "")
    to_date   = request.GET.get("to_date", "")

    selected_ledger  = None
    selected_farmer  = None
    selected_trader  = None
    entity_type      = None   # 'ledger' | 'farmer' | 'trader'
    lines            = []     # VoucherLine objects (ledger mode, raw)
    ledger_entries   = []     # enriched rows for ledger account statement
    entries          = []     # dicts with running balance (farmer/trader mode)
    opening_balance      = Decimal("0")
    opening_balance_type = "Dr"
    total_dr             = Decimal("0")
    total_cr             = Decimal("0")
    closing_balance      = Decimal("0")
    closing_balance_type = "Dr"
    dr_count             = 0
    cr_count             = 0

    if ledger_id:
        if ledger_id.startswith("farmer:"):
            farmer_pk = ledger_id.split(":", 1)[1]
            selected_farmer = get_object_or_404(Farmer, id=farmer_pk)
            entity_type = "farmer"
            opening_balance      = selected_farmer.opening_balance
            opening_balance_type = selected_farmer.balance_type

            # ── VoucherLine entries via linked LedgerAccount ────────────────
            try:
                linked_la = selected_farmer.ledger_account
                vl_qs = VoucherLine.objects.filter(ledger=linked_la).select_related(
                    "voucher", "voucher__ref_bikri__avak",
                    "voucher__ref_bikri__traderbillitem__bill",
                    "voucher__ref_trader_bill",
                )
                if from_date:
                    vl_qs = vl_qs.filter(voucher__date__gte=from_date)
                if to_date:
                    vl_qs = vl_qs.filter(voucher__date__lte=to_date)
                for line in vl_qs:
                    bill_no = _get_voucher_bill_no(line.voucher)
                    bill_info = _get_bill_info(line.voucher)
                    dr_amt, cr_amt = _party_voucher_dr_cr(line, "farmer")
                    entries.append({
                        "date":        line.voucher.date,
                        "type":        line.voucher.voucher_type,
                        "ref_no":      line.voucher.voucher_no,
                        "narration":   line.narration or line.voucher.narration or "-",
                        "bill_no":     bill_no,
                        "bill_info":   bill_info,
                        "raw_bill_no": bill_no,
                        "dr_amount":   dr_amt,
                        "cr_amount":   cr_amt,
                        "voucher_id":  line.voucher.id,
                        "extra_refs":  [],
                    })
            except LedgerAccount.DoesNotExist:
                pass

            # ── FinancialTransaction entries ────────────────────────────────
            ft_qs = FinancialTransaction.objects.filter(farmer=selected_farmer)
            if from_date:
                ft_qs = ft_qs.filter(date__gte=from_date)
            if to_date:
                ft_qs = ft_qs.filter(date__lte=to_date)
            for ft in ft_qs.order_by("date", "id"):
                bill_no = _ft_bill_no(ft)
                entries.append({
                    "date":      ft.date,
                    "type":      ft.voucher_type or ft.transaction_type,
                    "ref_no":    f"FT-{ft.id}",
                    "narration": ft.narration or ft.payment_method or "-",
                    "bill_no":   bill_no,
                    "bill_info": "",
                    "raw_bill_no": bill_no,
                    "dr_amount": ft.amount if ft.transaction_type == "Debit" else Decimal("0"),
                    "cr_amount": ft.amount if ft.transaction_type == "Credit" else Decimal("0"),
                    "ft_id":     ft.id,
                })

        elif ledger_id.startswith("trader:"):
            trader_pk = ledger_id.split(":", 1)[1]
            selected_trader = get_object_or_404(Trader, id=trader_pk)
            entity_type = "trader"
            opening_balance      = selected_trader.opening_balance
            opening_balance_type = selected_trader.balance_type

            # ── VoucherLine entries via linked LedgerAccount ────────────────
            try:
                linked_la = selected_trader.ledger_account
                vl_qs = VoucherLine.objects.filter(ledger=linked_la).select_related(
                    "voucher", "voucher__ref_bikri__avak",
                    "voucher__ref_bikri__traderbillitem__bill",
                    "voucher__ref_trader_bill",
                )
                if from_date:
                    vl_qs = vl_qs.filter(voucher__date__gte=from_date)
                if to_date:
                    vl_qs = vl_qs.filter(voucher__date__lte=to_date)
                for line in vl_qs:
                    bill_no = _get_voucher_bill_no(line.voucher)
                    bill_info = _get_bill_info(line.voucher)
                    dr_amt, cr_amt = _party_voucher_dr_cr(line, "trader")
                    entries.append({
                        "date":        line.voucher.date,
                        "type":        line.voucher.voucher_type,
                        "ref_no":      line.voucher.voucher_no,
                        "narration":   line.narration or line.voucher.narration or "-",
                        "bill_no":     bill_no,
                        "bill_info":   bill_info,
                        "raw_bill_no": bill_no,
                        "dr_amount":   dr_amt,
                        "cr_amount":   cr_amt,
                        "voucher_id":  line.voucher.id,
                        "extra_refs":  [],
                    })
            except LedgerAccount.DoesNotExist:
                pass

            # ── FinancialTransaction entries ────────────────────────────────
            ft_qs = FinancialTransaction.objects.filter(trader=selected_trader)
            if from_date:
                ft_qs = ft_qs.filter(date__gte=from_date)
            if to_date:
                ft_qs = ft_qs.filter(date__lte=to_date)
            for ft in ft_qs.order_by("date", "id"):
                bill_no = _ft_bill_no(ft)
                entries.append({
                    "date":      ft.date,
                    "type":      ft.voucher_type or ft.transaction_type,
                    "ref_no":    f"FT-{ft.id}",
                    "narration": ft.narration or ft.payment_method or "-",
                    "bill_no":   bill_no,
                    "bill_info": "",
                    "raw_bill_no": bill_no,
                    "dr_amount": ft.amount if ft.transaction_type == "Debit" else Decimal("0"),
                    "cr_amount": ft.amount if ft.transaction_type == "Credit" else Decimal("0"),
                    "ft_id":     ft.id,
                })

        else:
            selected_ledger = get_object_or_404(LedgerAccount, id=ledger_id)
            entity_type = "ledger"
            opening_balance      = selected_ledger.opening_balance
            opening_balance_type = selected_ledger.balance_type
            qs = VoucherLine.objects.filter(ledger=selected_ledger).select_related(
                "voucher", "voucher__ref_trader_bill", "voucher__ref_bikri"
            )
            if from_date:
                qs = qs.filter(voucher__date__gte=from_date)
            if to_date:
                qs = qs.filter(voucher__date__lte=to_date)
            lines = qs.order_by("voucher__date", "voucher__id")
            for line in lines:
                amt = line.amount
                if line.entry_type == "Dr":
                    total_dr += amt
                    dr_count += 1
                else:
                    total_cr += amt
                    cr_count += 1
                ledger_entries.append({
                    "date":         line.voucher.date,
                    "voucher_id":   line.voucher.id,
                    "voucher_no":   line.voucher.voucher_no,
                    "voucher_type": line.voucher.voucher_type,
                    "narration":    line.narration or line.voucher.narration or "-",
                    "bill_no":      _get_voucher_bill_no(line.voucher),
                    "entry_type":   line.entry_type,
                    "amount":       amt,
                })

        # ── Merge entries that share the same (date, bikri bill_no) ────────
        if entity_type in ("farmer", "trader"):
            entries.sort(key=lambda x: x["date"])
            merged = []
            bill_key_idx = {}  # (date, raw_bill_no) → index in merged
            for entry in entries:
                raw_bill = entry.get("raw_bill_no", "")
                if raw_bill and entry.get("voucher_id"):
                    key = (entry["date"], raw_bill)
                    if key in bill_key_idx:
                        existing = merged[bill_key_idx[key]]
                        existing["dr_amount"] += entry["dr_amount"]
                        existing["cr_amount"] += entry["cr_amount"]
                        existing["extra_refs"].append(entry["ref_no"])
                    else:
                        e = dict(entry)
                        e["extra_refs"] = []
                        bill_key_idx[key] = len(merged)
                        merged.append(e)
                else:
                    merged.append(entry)
            entries = merged

        # Compute running balance for farmer / trader entries
        if entity_type in ("farmer", "trader"):
            running_bal = opening_balance if opening_balance_type == "Dr" else -opening_balance
            dr_count = 0
            cr_count = 0
            for entry in entries:
                running_bal += entry["dr_amount"] - entry["cr_amount"]
                total_dr    += entry["dr_amount"]
                total_cr    += entry["cr_amount"]
                if entry["dr_amount"] > 0:
                    dr_count += 1
                if entry["cr_amount"] > 0:
                    cr_count += 1
                entry["running_balance"]      = abs(running_bal)
                entry["running_balance_type"] = "Dr" if running_bal >= 0 else "Cr"
            closing_balance      = abs(running_bal)
            closing_balance_type = "Dr" if running_bal >= 0 else "Cr"
        elif entity_type == "ledger" and ledger_entries:
            running_bal = opening_balance if opening_balance_type == "Dr" else -opening_balance
            for row in ledger_entries:
                if row["entry_type"] == "Dr":
                    running_bal += row["amount"]
                else:
                    running_bal -= row["amount"]
                row["running_balance"] = abs(running_bal)
                row["running_balance_type"] = "Dr" if running_bal >= 0 else "Cr"
            closing_balance = abs(running_bal)
            closing_balance_type = "Dr" if running_bal >= 0 else "Cr"

    return render(request, "accounts/ledger_book.html", {
        "ledger_accounts":       ledger_accounts,
        "farmers":               farmers,
        "traders":               traders,
        "selected_ledger":       selected_ledger,
        "selected_farmer":       selected_farmer,
        "selected_trader":       selected_trader,
        "entity_type":           entity_type,
        "lines":                 lines,
        "ledger_entries":        ledger_entries,
        "entries":               entries,
        "opening_balance":       opening_balance,
        "opening_balance_type":  opening_balance_type,
        "total_dr":              total_dr,
        "total_cr":              total_cr,
        "dr_count":              dr_count,
        "cr_count":              cr_count,
        "closing_balance":       closing_balance,
        "closing_balance_type":  closing_balance_type,
        "from_date":             from_date,
        "to_date":               to_date,
        "ledger_id":             ledger_id,
        "today":                 date.today().strftime("%Y-%m-%d"),
    })


@login_required
def ledger_monthwise(request):
    """Monthwise summary report (April–March) — separate from account statement."""
    ensure_default_ledgers()
    ledger_accounts = LedgerAccount.objects.select_related("group").order_by("group__nature", "name")
    farmers = Farmer.objects.order_by("name")
    traders = Trader.objects.order_by("name")

    ledger_id = request.GET.get("ledger_id", "")
    try:
        fy_start = int(request.GET.get("fy", _current_financial_year_start()))
    except (TypeError, ValueError):
        fy_start = _current_financial_year_start()
    fy_end = fy_start + 1

    selected_ledger = None
    selected_farmer = None
    selected_trader = None
    entity_type = None
    monthwise_rows = []
    account_display_name = ""

    if ledger_id:
        if ledger_id.startswith("farmer:"):
            selected_farmer = get_object_or_404(Farmer, id=ledger_id.split(":", 1)[1])
            entity_type = "farmer"
        elif ledger_id.startswith("trader:"):
            selected_trader = get_object_or_404(Trader, id=ledger_id.split(":", 1)[1])
            entity_type = "trader"
        else:
            selected_ledger = get_object_or_404(LedgerAccount, id=ledger_id)
            entity_type = "ledger"

        account_display_name, monthwise_rows = _build_monthwise_for_selection(
            entity_type, selected_ledger, selected_farmer, selected_trader, fy_start
        )

    fy_choices = list(range(_current_financial_year_start() - 5, _current_financial_year_start() + 2))

    return render(request, "accounts/ledger_monthwise.html", {
        "ledger_accounts": ledger_accounts,
        "farmers": farmers,
        "traders": traders,
        "selected_ledger": selected_ledger,
        "selected_farmer": selected_farmer,
        "selected_trader": selected_trader,
        "entity_type": entity_type,
        "ledger_id": ledger_id,
        "fy_start": fy_start,
        "fy_end": fy_end,
        "fy_label": f"April {fy_start} – March {fy_end}",
        "fy_choices": fy_choices,
        "monthwise_rows": monthwise_rows,
        "account_display_name": account_display_name,
    })


@login_required
def account_statement(request):
    """Comprehensive account statement – combines Voucher lines AND
    FinancialTransaction entries for the selected ledger with running balance."""
    ledger_accounts = LedgerAccount.objects.select_related("group").order_by("group__nature", "name")
    ledger_id   = request.GET.get("ledger_id")
    from_date   = request.GET.get("from_date", "")
    to_date     = request.GET.get("to_date", "")

    selected_ledger      = None
    entries              = []
    opening_balance      = Decimal("0")
    opening_balance_type = "Dr"
    total_dr = Decimal("0")
    total_cr = Decimal("0")
    closing_balance      = Decimal("0")
    closing_balance_type = "Dr"

    if ledger_id:
        selected_ledger      = get_object_or_404(LedgerAccount, id=ledger_id)
        opening_balance      = selected_ledger.opening_balance
        opening_balance_type = selected_ledger.balance_type

        # ── 1. VoucherLine entries ──────────────────────────────────────────
        vl_qs = VoucherLine.objects.filter(ledger=selected_ledger).select_related(
            "voucher", "voucher__ref_trader_bill", "voucher__ref_bikri"
        )
        if from_date:
            vl_qs = vl_qs.filter(voucher__date__gte=from_date)
        if to_date:
            vl_qs = vl_qs.filter(voucher__date__lte=to_date)

        for line in vl_qs:
            if selected_ledger.farmer_id:
                dr_amt, cr_amt = _party_voucher_dr_cr(line, "farmer")
            elif selected_ledger.trader_id:
                dr_amt, cr_amt = _party_voucher_dr_cr(line, "trader")
            else:
                dr_amt = line.amount if line.entry_type == "Dr" else Decimal("0")
                cr_amt = line.amount if line.entry_type == "Cr" else Decimal("0")
            entries.append({
                "date":       line.voucher.date,
                "type":       line.voucher.voucher_type,
                "ref_no":     line.voucher.voucher_no,
                "bill_no":    _get_voucher_bill_no(line.voucher),
                "narration":  line.narration or line.voucher.narration or "-",
                "dr_amount":  dr_amt,
                "cr_amount":  cr_amt,
                "source":     "voucher",
                "voucher_id": line.voucher.id,
            })

        # ── 2. FinancialTransaction where pay_from_ledger = this ledger ─────
        ft_qs = FinancialTransaction.objects.filter(
            pay_from_ledger=selected_ledger
        ).select_related("farmer", "trader", "pay_from_ledger")
        if from_date:
            ft_qs = ft_qs.filter(date__gte=from_date)
        if to_date:
            ft_qs = ft_qs.filter(date__lte=to_date)

        existing_ft_ids = set()
        for ft in ft_qs:
            # pay_from_ledger is the Cash/Bank account.
            # Debit transaction (payment out) → money leaves → Credit this ledger
            # Credit transaction (receipt in) → money arrives → Debit this ledger
            if ft.transaction_type == "Debit":
                dr_amt = Decimal("0")
                cr_amt = ft.amount
            else:
                dr_amt = ft.amount
                cr_amt = Decimal("0")

            person_name = (
                ft.farmer.name if ft.farmer else
                ft.trader.name if ft.trader else
                ft.name or ""
            )
            narration = ft.narration or f"{ft.voucher_type or ft.transaction_type} – {person_name}".strip(" –")

            entries.append({
                "date":      ft.date,
                "type":      ft.voucher_type or ft.transaction_type,
                "ref_no":    f"FT-{ft.id}",
                "bill_no":   _ft_bill_no(ft),
                "narration": narration or "-",
                "dr_amount": dr_amt,
                "cr_amount": cr_amt,
                "source":    "ft",
                "ft_id":     ft.id,
            })
            existing_ft_ids.add(ft.id)

        # ── 3. FinancialTransaction where debit_ledger = this ledger ────────
        ft_dr_qs = FinancialTransaction.objects.filter(
            debit_ledger=selected_ledger
        ).select_related("farmer", "trader", "debit_ledger")
        if from_date:
            ft_dr_qs = ft_dr_qs.filter(date__gte=from_date)
        if to_date:
            ft_dr_qs = ft_dr_qs.filter(date__lte=to_date)

        for ft in ft_dr_qs:
            if ft.id in existing_ft_ids:
                continue  # already captured above
            person_name = (
                ft.farmer.name if ft.farmer else
                ft.trader.name if ft.trader else
                ft.name or ""
            )
            narration = ft.narration or f"{ft.voucher_type or ft.transaction_type} – {person_name}".strip(" –")
            entries.append({
                "date":      ft.date,
                "type":      ft.voucher_type or ft.transaction_type,
                "ref_no":    f"FT-{ft.id}",
                "bill_no":   _ft_bill_no(ft),
                "narration": narration or "-",
                "dr_amount": ft.amount,
                "cr_amount": Decimal("0"),
                "source":    "ft_debit",
                "ft_id":     ft.id,
            })
            existing_ft_ids.add(ft.id)

        # ── 4. Party payments/receipts on farmer or trader ledger ───────────
        if selected_ledger.farmer_id:
            ft_party_qs = FinancialTransaction.objects.filter(
                farmer_id=selected_ledger.farmer_id
            ).select_related("farmer", "pay_from_ledger")
            if from_date:
                ft_party_qs = ft_party_qs.filter(date__gte=from_date)
            if to_date:
                ft_party_qs = ft_party_qs.filter(date__lte=to_date)
            for ft in ft_party_qs:
                if ft.id in existing_ft_ids:
                    continue
                person_name = ft.farmer.name if ft.farmer else (ft.name or "")
                narration = ft.narration or f"{ft.voucher_type or ft.transaction_type} – {person_name}".strip(" –")
                if ft.transaction_type == "Debit":
                    dr_amt, cr_amt = ft.amount, Decimal("0")
                else:
                    dr_amt, cr_amt = Decimal("0"), ft.amount
                entries.append({
                    "date": ft.date,
                    "type": ft.voucher_type or ft.transaction_type,
                    "ref_no": f"FT-{ft.id}",
                    "bill_no": _ft_bill_no(ft),
                    "narration": narration or "-",
                    "dr_amount": dr_amt,
                    "cr_amount": cr_amt,
                    "source": "ft_party",
                    "ft_id": ft.id,
                })
                existing_ft_ids.add(ft.id)
        elif selected_ledger.trader_id:
            ft_party_qs = FinancialTransaction.objects.filter(
                trader_id=selected_ledger.trader_id
            ).select_related("trader", "pay_from_ledger")
            if from_date:
                ft_party_qs = ft_party_qs.filter(date__gte=from_date)
            if to_date:
                ft_party_qs = ft_party_qs.filter(date__lte=to_date)
            for ft in ft_party_qs:
                if ft.id in existing_ft_ids:
                    continue
                person_name = ft.trader.name if ft.trader else (ft.name or "")
                narration = ft.narration or f"{ft.voucher_type or ft.transaction_type} – {person_name}".strip(" –")
                if ft.transaction_type == "Debit":
                    dr_amt, cr_amt = ft.amount, Decimal("0")
                else:
                    dr_amt, cr_amt = Decimal("0"), ft.amount
                entries.append({
                    "date": ft.date,
                    "type": ft.voucher_type or ft.transaction_type,
                    "ref_no": f"FT-{ft.id}",
                    "bill_no": _ft_bill_no(ft),
                    "narration": narration or "-",
                    "dr_amount": dr_amt,
                    "cr_amount": cr_amt,
                    "source": "ft_party",
                    "ft_id": ft.id,
                })
                existing_ft_ids.add(ft.id)

        # ── Sort chronologically ─────────────────────────────────────────────
        entries.sort(key=lambda x: x["date"])

        # ── Running balance ──────────────────────────────────────────────────
        # Dr balance = positive, Cr balance = negative (for calculation)
        running_bal = opening_balance if opening_balance_type == "Dr" else -opening_balance

        for entry in entries:
            running_bal += entry["dr_amount"] - entry["cr_amount"]
            total_dr    += entry["dr_amount"]
            total_cr    += entry["cr_amount"]
            entry["running_balance"]      = abs(running_bal)
            entry["running_balance_type"] = "Dr" if running_bal >= 0 else "Cr"

        closing_balance      = abs(running_bal)
        closing_balance_type = "Dr" if running_bal >= 0 else "Cr"

    return render(request, "accounts/account_statement.html", {
        "ledger_accounts":       ledger_accounts,
        "selected_ledger":       selected_ledger,
        "entries":               entries,
        "opening_balance":       opening_balance,
        "opening_balance_type":  opening_balance_type,
        "total_dr":              total_dr,
        "total_cr":              total_cr,
        "closing_balance":       closing_balance,
        "closing_balance_type":  closing_balance_type,
        "from_date":             from_date,
        "to_date":               to_date,
        "today":                 date.today().strftime("%Y-%m-%d"),
    })


# ─────────────────────────────────────────────────────────────────────────────
# NEW TALLY VOUCHERS  (Payment + Receipt for Both Farmers & Traders)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def tally_payment_view(request):
    """Tally-style Payment Voucher – supports Farmers and Traders."""
    cash_bank_accounts = _cash_bank_ledgers_qs()

    VOUCHER_MAP = {
        "Cash Payment":      ("Debit", "Cash"),
        "Cheque Payment":    ("Debit", "Cheque"),
        "NEFT/RTGS Payment": ("Debit", "NEFT"),
    }

    if request.method == "POST":
        date_str     = request.POST.get("date")
        voucher_type = request.POST.get("voucher_type", "Cash Payment")
        transaction_type, payment_method = VOUCHER_MAP.get(voucher_type, ("Debit", "Cash"))

        person_type        = request.POST.get("person_type", "Farmer")
        farmer_id          = request.POST.get("farmer_id")
        trader_id          = request.POST.get("trader_id")
        name               = request.POST.get("name", "").strip()
        place              = request.POST.get("place", "").strip()
        phone_number       = request.POST.get("phone_number", "").strip()
        pay_from_ledger_id = request.POST.get("pay_from_ledger_id")
        cheque_no          = request.POST.get("cheque_no", "").strip() or None
        cheque_bank_name   = request.POST.get("cheque_bank_name", "").strip() or None
        narration          = request.POST.get("narration", "").strip()
        amount             = _to_decimal(request.POST.get("amount"), default="0")

        if not pay_from_ledger_id or not pay_from_ledger_id.isdigit():
            messages.error(request, "Please select a Cash / Bank account.")
        elif amount <= 0:
            messages.error(request, "Amount must be greater than zero.")
        else:
            FinancialTransaction.objects.create(
                date=date_str or date.today(),
                transaction_type=transaction_type,
                voucher_type=voucher_type,
                person_type=person_type,
                farmer_id=int(farmer_id) if farmer_id and farmer_id.isdigit() else None,
                trader_id=int(trader_id) if trader_id and trader_id.isdigit() else None,
                name=name,
                place=place,
                phone_number=phone_number,
                payment_method=payment_method,
                pay_from_ledger_id=int(pay_from_ledger_id),
                cheque_no=cheque_no,
                cheque_bank_name=cheque_bank_name,
                narration=narration,
                amount=amount,
            )
            messages.success(request, f"{voucher_type} entry saved successfully.")
            entry_date = date_str or date.today().strftime("%Y-%m-%d")
            from django.urls import reverse as _rev
            return redirect(_rev("tally_payment_list") + f"?from_date={entry_date}&to_date={entry_date}")

    return render(request, "accounts/tally_payment.html", {
        "today":              date.today().strftime("%Y-%m-%d"),
        "farmers":            Farmer.objects.all().order_by("name"),
        "traders":            Trader.objects.all().order_by("name"),
        "cash_bank_accounts": cash_bank_accounts,
    })


@login_required
def tally_payment_list(request):
    from_date = request.GET.get("from_date", "")
    to_date   = request.GET.get("to_date", "")
    today_str = date.today().strftime("%Y-%m-%d")
    if from_date or to_date:
        payments = FinancialTransaction.objects.filter(
            transaction_type="Debit",
            voucher_type__in=["Cash Payment", "Cheque Payment", "NEFT/RTGS Payment"]
        ).select_related("farmer", "trader", "pay_from_ledger").order_by("-date", "-created_at")
        if from_date:
            payments = payments.filter(date__gte=from_date)
        if to_date:
            payments = payments.filter(date__lte=to_date)
    else:
        payments = FinancialTransaction.objects.none()
    return render(request, "accounts/tally_payment_list.html", {
        "payments":   payments,
        "from_date":  from_date,
        "to_date":    to_date,
        "today":      today_str,
    })


@login_required
def tally_receipt_view(request):
    """Tally-style Receipt Voucher – supports Farmers and Traders."""
    cash_bank_accounts = _cash_bank_ledgers_qs()

    VOUCHER_MAP = {
        "Cash Receipt":      ("Credit", "Cash"),
        "Cheque Receipt":    ("Credit", "Cheque"),
        "NEFT/RTGS Receipt": ("Credit", "NEFT"),
    }

    if request.method == "POST":
        date_str     = request.POST.get("date")
        voucher_type = request.POST.get("voucher_type", "Cash Receipt")
        transaction_type, payment_method = VOUCHER_MAP.get(voucher_type, ("Credit", "Cash"))

        person_type        = request.POST.get("person_type", "Trader")
        farmer_id          = request.POST.get("farmer_id")
        trader_id          = request.POST.get("trader_id")
        name               = request.POST.get("name", "").strip()
        place              = request.POST.get("place", "").strip()
        phone_number       = request.POST.get("phone_number", "").strip()
        pay_from_ledger_id = request.POST.get("pay_from_ledger_id")
        cheque_no          = request.POST.get("cheque_no", "").strip() or None
        cheque_bank_name   = request.POST.get("cheque_bank_name", "").strip() or None
        narration          = request.POST.get("narration", "").strip()
        amount             = _to_decimal(request.POST.get("amount"), default="0")

        if not pay_from_ledger_id or not pay_from_ledger_id.isdigit():
            messages.error(request, "Please select a Cash / Bank account.")
        elif amount <= 0:
            messages.error(request, "Amount must be greater than zero.")
        else:
            FinancialTransaction.objects.create(
                date=date_str or date.today(),
                transaction_type=transaction_type,
                voucher_type=voucher_type,
                person_type=person_type,
                farmer_id=int(farmer_id) if farmer_id and farmer_id.isdigit() else None,
                trader_id=int(trader_id) if trader_id and trader_id.isdigit() else None,
                name=name,
                place=place,
                phone_number=phone_number,
                payment_method=payment_method,
                pay_from_ledger_id=int(pay_from_ledger_id),
                cheque_no=cheque_no,
                cheque_bank_name=cheque_bank_name,
                narration=narration,
                amount=amount,
            )
            messages.success(request, f"{voucher_type} entry saved successfully.")
            entry_date = date_str or date.today().strftime("%Y-%m-%d")
            from django.urls import reverse as _rev
            return redirect(_rev("tally_receipt_list") + f"?from_date={entry_date}&to_date={entry_date}")

    return render(request, "accounts/tally_receipt.html", {
        "today":              date.today().strftime("%Y-%m-%d"),
        "farmers":            Farmer.objects.all().order_by("name"),
        "traders":            Trader.objects.all().order_by("name"),
        "cash_bank_accounts": cash_bank_accounts,
    })


@login_required
def tally_receipt_list(request):
    from_date = request.GET.get("from_date", "")
    to_date   = request.GET.get("to_date", "")
    today_str = date.today().strftime("%Y-%m-%d")
    if from_date or to_date:
        receipts = FinancialTransaction.objects.filter(
            transaction_type="Credit",
            voucher_type__in=["Cash Receipt", "Cheque Receipt", "NEFT/RTGS Receipt"]
        ).select_related("farmer", "trader", "pay_from_ledger").order_by("-date", "-created_at")
        if from_date:
            receipts = receipts.filter(date__gte=from_date)
        if to_date:
            receipts = receipts.filter(date__lte=to_date)
    else:
        receipts = FinancialTransaction.objects.none()
    return render(request, "accounts/tally_receipt_list.html", {
        "receipts":  receipts,
        "from_date": from_date,
        "to_date":   to_date,
        "today":     today_str,
    })


# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED ACCOUNT STATEMENT  (All Farmers + Traders + Expenses + Bank)
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def unified_ledger(request):
    """
    Consolidated ledger showing ALL accounts:
    - Farmer accounts (from Bikri + FinancialTransactions)
    - Trader accounts (from TraderBill + FinancialTransactions)
    - Expense / Income LedgerAccounts (from VoucherLines)
    - Bank / Cash Accounts (from VoucherLines + FinancialTransactions)
    Filtered by date range. Summary table + per-section detail.
    """
    from collections import defaultdict
    from .models import TraderBill

    from_date_str = request.GET.get("from_date", "")
    to_date_str   = request.GET.get("to_date", "")
    show_section  = request.GET.get("section", "all")   # all | farmers | traders | expenses | bank
    today_str     = date.today().strftime("%Y-%m-%d")

    # ── helpers ──────────────────────────────────────────────────────────────
    def _ft_qs(base_qs):
        if from_date_str:
            base_qs = base_qs.filter(date__gte=from_date_str)
        if to_date_str:
            base_qs = base_qs.filter(date__lte=to_date_str)
        return base_qs

    def _vl_qs(base_qs):
        if from_date_str:
            base_qs = base_qs.filter(voucher__date__gte=from_date_str)
        if to_date_str:
            base_qs = base_qs.filter(voucher__date__lte=to_date_str)
        return base_qs

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 1: FARMERS
    # ════════════════════════════════════════════════════════════════════════
    farmer_rows = []
    if show_section in ("all", "farmers"):
        for farmer in Farmer.objects.all().order_by("name"):
            # Opening balance
            ob = farmer.opening_balance
            ob_type = farmer.balance_type  # Cr = farmer is owed money
            running = ob if ob_type == "Cr" else -ob   # Cr positive = market owes farmer

            entries = []

            # Bikri entries: net payable credited to farmer
            bikris = _ft_qs(
                Bikri.objects.filter(avak__farmer=farmer, is_cancelled=False)
                .select_related("avak")
                .order_by("date")
            )
            for b in bikris:
                running += b.net_payable
                entries.append({
                    "date":    b.date,
                    "type":    "Vikri",
                    "ref_no":  f"Lot {b.avak.lot_number}",
                    "bill_no": (b.bill_no or "").strip(),
                    "narration": f"ವಿಕ್ರಿ – Lot {b.avak.lot_number}",
                    "credit":  b.net_payable,
                    "debit":   Decimal("0"),
                    "balance": abs(running),
                    "bal_type": "Cr" if running >= 0 else "Dr",
                })

            # FinancialTransaction (payments made to farmer = debit reduces balance)
            fts = _ft_qs(
                FinancialTransaction.objects.filter(farmer=farmer)
                .select_related("pay_from_ledger")
                .order_by("date")
            )
            for ft in fts:
                if ft.transaction_type == "Debit":
                    running -= ft.amount
                    dr, cr = ft.amount, Decimal("0")
                else:
                    running += ft.amount
                    dr, cr = Decimal("0"), ft.amount
                entries.append({
                    "date":    ft.date,
                    "type":    ft.voucher_type or ft.transaction_type,
                    "ref_no":  f"FT-{ft.id}",
                    "bill_no": _ft_bill_no(ft),
                    "narration": ft.narration or (ft.voucher_type or ft.transaction_type),
                    "debit":   dr,
                    "credit":  cr,
                    "balance": abs(running),
                    "bal_type": "Cr" if running >= 0 else "Dr",
                })

            entries.sort(key=lambda x: x["date"])
            # Recalculate running balance in order
            running2 = ob if ob_type == "Cr" else -ob
            for e in entries:
                running2 += e["credit"] - e["debit"]
                e["balance"]  = abs(running2)
                e["bal_type"] = "Cr" if running2 >= 0 else "Dr"

            total_dr = sum(e["debit"]  for e in entries)
            total_cr = sum(e["credit"] for e in entries)
            closing  = abs(running2)
            closing_type = "Cr" if running2 >= 0 else "Dr"

            farmer_rows.append({
                "name":       farmer.name,
                "id":         farmer.id,
                "phone":      farmer.phone or "",
                "ob":         ob,
                "ob_type":    ob_type,
                "entries":    entries,
                "total_dr":   total_dr,
                "total_cr":   total_cr,
                "closing":    closing,
                "closing_type": closing_type,
            })

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 2: TRADERS
    # ════════════════════════════════════════════════════════════════════════
    trader_rows = []
    if show_section in ("all", "traders"):
        for trader in Trader.objects.all().order_by("name"):
            ob = trader.opening_balance
            ob_type = trader.balance_type   # Dr = trader owes market
            running = ob if ob_type == "Dr" else -ob

            entries = []

            # TraderBill entries: trader owes grand_total
            bills = TraderBill.objects.filter(buyer=trader).order_by("date")
            if from_date_str:
                bills = bills.filter(date__gte=from_date_str)
            if to_date_str:
                bills = bills.filter(date__lte=to_date_str)
            for bill in bills:
                running += bill.grand_total
                entries.append({
                    "date":    bill.date,
                    "type":    "ಖರೀದಿ ಪಟ್ಟಿ",
                    "ref_no":  bill.invoice_no,
                    "bill_no": str(bill.invoice_no),
                    "narration": f"Invoice {bill.invoice_no}",
                    "debit":   bill.grand_total,
                    "credit":  Decimal("0"),
                    "balance": abs(running),
                    "bal_type": "Dr" if running >= 0 else "Cr",
                })

            # FinancialTransaction (receipts from trader reduce their balance)
            fts = _ft_qs(
                FinancialTransaction.objects.filter(trader=trader)
                .select_related("pay_from_ledger")
                .order_by("date")
            )
            for ft in fts:
                if ft.transaction_type == "Credit":
                    running -= ft.amount
                    dr, cr = Decimal("0"), ft.amount
                else:
                    running += ft.amount
                    dr, cr = ft.amount, Decimal("0")
                entries.append({
                    "date":    ft.date,
                    "type":    ft.voucher_type or ft.transaction_type,
                    "ref_no":  f"FT-{ft.id}",
                    "bill_no": _ft_bill_no(ft),
                    "narration": ft.narration or (ft.voucher_type or ft.transaction_type),
                    "debit":   dr,
                    "credit":  cr,
                    "balance": abs(running),
                    "bal_type": "Dr" if running >= 0 else "Cr",
                })

            entries.sort(key=lambda x: x["date"])
            running2 = ob if ob_type == "Dr" else -ob
            for e in entries:
                running2 += e["debit"] - e["credit"]
                e["balance"]  = abs(running2)
                e["bal_type"] = "Dr" if running2 >= 0 else "Cr"

            total_dr = sum(e["debit"]  for e in entries)
            total_cr = sum(e["credit"] for e in entries)
            closing  = abs(running2)
            closing_type = "Dr" if running2 >= 0 else "Cr"

            trader_rows.append({
                "name":       trader.name,
                "id":         trader.id,
                "phone":      trader.phone or "",
                "ob":         ob,
                "ob_type":    ob_type,
                "entries":    entries,
                "total_dr":   total_dr,
                "total_cr":   total_cr,
                "closing":    closing,
                "closing_type": closing_type,
            })

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 3: EXPENSE / INCOME LEDGER ACCOUNTS
    # ════════════════════════════════════════════════════════════════════════
    expense_rows = []
    if show_section in ("all", "expenses"):
        expense_ledgers = LedgerAccount.objects.filter(
            Q(group__nature__in=["Expenses", "Income"])
            | Q(group__name="Other Liabilities")
        ).select_related("group").order_by("group__nature", "name")

        for la in expense_ledgers:
            ob = la.opening_balance
            ob_type = la.balance_type
            running = ob if ob_type == "Dr" else -ob

            vl_qs_base = _vl_qs(
                VoucherLine.objects.filter(ledger=la).select_related(
                    "voucher", "voucher__ref_trader_bill", "voucher__ref_bikri"
                )
            ).order_by("voucher__date")

            entries = []
            for vl in vl_qs_base:
                if vl.entry_type == "Dr":
                    running += vl.amount
                    dr, cr = vl.amount, Decimal("0")
                else:
                    running -= vl.amount
                    dr, cr = Decimal("0"), vl.amount
                entries.append({
                    "date":     vl.voucher.date,
                    "type":     vl.voucher.voucher_type,
                    "ref_no":   vl.voucher.voucher_no,
                    "bill_no":  _get_voucher_bill_no(vl.voucher),
                    "narration": vl.narration or vl.voucher.narration or "-",
                    "debit":    dr,
                    "credit":   cr,
                    "balance":  abs(running),
                    "bal_type": "Dr" if running >= 0 else "Cr",
                })

            total_dr = sum(e["debit"]  for e in entries)
            total_cr = sum(e["credit"] for e in entries)
            closing  = abs(running)
            closing_type = "Dr" if running >= 0 else "Cr"

            expense_rows.append({
                "name":        la.name,
                "group":       la.group.name,
                "nature":      la.group.nature,
                "ob":          ob,
                "ob_type":     ob_type,
                "entries":     entries,
                "total_dr":    total_dr,
                "total_cr":    total_cr,
                "closing":     closing,
                "closing_type": closing_type,
            })

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 4: BANK / CASH ACCOUNTS
    # ════════════════════════════════════════════════════════════════════════
    bank_rows = []
    if show_section in ("all", "bank"):
        bank_ledgers = _cash_bank_ledgers_qs()

        for la in bank_ledgers:
            ob = la.opening_balance
            ob_type = la.balance_type
            running = ob if ob_type == "Dr" else -ob

            entries = []

            # VoucherLine entries
            for vl in _vl_qs(
                VoucherLine.objects.filter(ledger=la).select_related(
                    "voucher", "voucher__ref_trader_bill", "voucher__ref_bikri"
                )
            ).order_by("voucher__date"):
                if vl.entry_type == "Dr":
                    running += vl.amount
                    dr, cr = vl.amount, Decimal("0")
                else:
                    running -= vl.amount
                    dr, cr = Decimal("0"), vl.amount
                entries.append({
                    "date":     vl.voucher.date,
                    "type":     vl.voucher.voucher_type,
                    "ref_no":   vl.voucher.voucher_no,
                    "bill_no":  _get_voucher_bill_no(vl.voucher),
                    "narration": vl.narration or vl.voucher.narration or "-",
                    "debit":    dr,
                    "credit":   cr,
                    "source":   "voucher",
                })

            # FinancialTransaction entries using this bank
            seen_ft_ids = set()
            for ft in _ft_qs(
                FinancialTransaction.objects.filter(pay_from_ledger=la)
                .select_related("farmer", "trader")
            ).order_by("date"):
                seen_ft_ids.add(ft.id)
                person = ft.farmer.name if ft.farmer else (ft.trader.name if ft.trader else ft.name or "")
                narr = ft.narration or f"{ft.voucher_type or ft.transaction_type} – {person}".strip(" –")
                if ft.transaction_type == "Debit":
                    running -= ft.amount
                    dr, cr = Decimal("0"), ft.amount  # money out = Cr for bank
                else:
                    running += ft.amount
                    dr, cr = ft.amount, Decimal("0")  # money in = Dr for bank
                entries.append({
                    "date":     ft.date,
                    "type":     ft.voucher_type or ft.transaction_type,
                    "ref_no":   f"FT-{ft.id}",
                    "bill_no":  _ft_bill_no(ft),
                    "narration": narr,
                    "debit":    dr,
                    "credit":   cr,
                    "source":   "ft",
                })

            entries.sort(key=lambda x: x["date"])
            # Rebuild running balance in order
            running2 = ob if ob_type == "Dr" else -ob
            for e in entries:
                running2 += e["debit"] - e["credit"]
                e["balance"]  = abs(running2)
                e["bal_type"] = "Dr" if running2 >= 0 else "Cr"

            total_dr = sum(e["debit"]  for e in entries)
            total_cr = sum(e["credit"] for e in entries)
            closing  = abs(running2)
            closing_type = "Dr" if running2 >= 0 else "Cr"

            bank_rows.append({
                "name":        la.name,
                "group":       la.group.name,
                "ob":          ob,
                "ob_type":     ob_type,
                "entries":     entries,
                "total_dr":    total_dr,
                "total_cr":    total_cr,
                "closing":     closing,
                "closing_type": closing_type,
            })

    # ── Grand totals ─────────────────────────────────────────────────────────
    grand_farmer_balance = sum(r["closing"] for r in farmer_rows)
    grand_trader_balance = sum(r["closing"] for r in trader_rows)
    grand_expense_dr     = sum(r["total_dr"] for r in expense_rows)
    grand_expense_cr     = sum(r["total_cr"] for r in expense_rows)

    return render(request, "accounts/unified_ledger.html", {
        "farmer_rows":          farmer_rows,
        "trader_rows":          trader_rows,
        "expense_rows":         expense_rows,
        "bank_rows":            bank_rows,
        "from_date":            from_date_str,
        "to_date":              to_date_str,
        "today":                today_str,
        "show_section":         show_section,
        "grand_farmer_balance": grand_farmer_balance,
        "grand_trader_balance": grand_trader_balance,
        "grand_expense_dr":     grand_expense_dr,
        "grand_expense_cr":     grand_expense_cr,
        "section_choices": [
            ("all",      "All Accounts",      "fas fa-layer-group",   "dark"),
            ("farmers",  "Farmers",           "fas fa-seedling",      "success"),
            ("traders",  "Traders",           "fas fa-store",         "primary"),
            ("expenses", "Expenses / Income", "fas fa-receipt",       "warning"),
            ("bank",     "Bank / Cash",       "fas fa-university",    "info"),
        ],
    })









