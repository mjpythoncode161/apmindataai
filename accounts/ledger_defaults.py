"""
Default ledger groups and accounts for the Tally voucher system.

`ensure_default_ledgers()` is safe to call anytime: it only creates or updates
missing defaults — it never deletes user data.
"""

from accounts.models import LedgerGroup, LedgerAccount

# Tally-style trial balance heads (name, nature)
DEFAULT_GROUPS = [
    ("Bank Accounts", "Assets"),
    ("Bank OD A/C", "Assets"),
    ("Current Assets", "Assets"),
    ("Sundry Debtors", "Assets"),
    ("Capital Account", "Liabilities"),
    ("Sundry Creditors", "Liabilities"),
    ("Unsecured Loan", "Liabilities"),
    ("Provision(Payable)", "Liabilities"),
    ("Others", "Liabilities"),
    ("Direct Incomes", "Income"),
    ("Trading Account", "Income"),
    ("Indirect Expenses", "Expenses"),
    # Legacy groups (kept for Group / Category dropdown and older ledgers)
    ("Cash & Bank", "Assets"),
    ("Fixed Assets", "Assets"),
    ("Other Assets", "Assets"),
    ("Loans & Borrowings", "Liabilities"),
    ("Other Liabilities", "Liabilities"),
    ("Commission Income", "Income"),
    ("Other Income", "Income"),
    ("Rent & Rates", "Expenses"),
    ("Electricity & Water", "Expenses"),
    ("Salary & Wages", "Expenses"),
    ("Office Expenses", "Expenses"),
    ("Communication", "Expenses"),
    ("Printing & Stationery", "Expenses"),
    ("Repairs & Maintenance", "Expenses"),
    ("Transportation", "Expenses"),
    ("Miscellaneous", "Expenses"),
    ("Expenses", "Expenses"),
]

DEFAULT_BANK_ACCOUNTS = (
    "SBI",
    "HDFC Bank",
    "ICICI Bank",
    "Canara Bank",
    "Karnataka Bank",
    "Union Bank",
    "Bank of Baroda",
    "Indian Bank",
    "Axis Bank",
    "IDBI Bank",
)

DEFAULT_ACCOUNTS = [
    # (name, group_name, balance_type, is_system)
    ("Cash in Hand", "Bank Accounts", "Dr", True),
    *[(name, "Bank Accounts", "Dr", True) for name in DEFAULT_BANK_ACCOUNTS],
    ("Commission Account", "Direct Incomes", "Cr", True),
    ("Weighman Fee Income", "Direct Incomes", "Cr", True),
    ("Hamali", "Direct Incomes", "Cr", True),
    ("Packing Income", "Direct Incomes", "Cr", True),
    ("Farmer Deductions Income", "Direct Incomes", "Cr", True),
    ("Bazar Sales Receivable", "Sundry Debtors", "Dr", True),
    ("Cess Income", "Provision(Payable)", "Cr", True),
    ("Output SGST", "Provision(Payable)", "Cr", True),
    ("Output CGST", "Provision(Payable)", "Cr", True),
    ("GST Payable", "Provision(Payable)", "Cr", True),
    ("Round Off", "Indirect Expenses", "Dr", True),
    ("Bank Charges", "Indirect Expenses", "Dr", False),
    ("Od Intrest", "Indirect Expenses", "Dr", False),
    ("Software Service Charge", "Indirect Expenses", "Dr", False),
    ("Audit Fee", "Indirect Expenses", "Dr", False),
    ("Rent Expense", "Indirect Expenses", "Dr", False),
    ("Municipal Tax", "Indirect Expenses", "Dr", False),
    ("Electricity Bill", "Indirect Expenses", "Dr", False),
    ("Water Charges", "Indirect Expenses", "Dr", False),
    ("Staff Salaries", "Indirect Expenses", "Dr", False),
    ("Daily Wages", "Indirect Expenses", "Dr", False),
    ("Stationery & Printing", "Indirect Expenses", "Dr", False),
    ("Office Maintenance", "Indirect Expenses", "Dr", False),
    ("Telephone / Mobile Bill", "Indirect Expenses", "Dr", False),
    ("Internet Charges", "Indirect Expenses", "Dr", False),
    ("Vehicle Fuel", "Indirect Expenses", "Dr", False),
    ("Vehicle Maintenance", "Indirect Expenses", "Dr", False),
    ("Building Repairs", "Indirect Expenses", "Dr", False),
    ("Equipment Repairs", "Indirect Expenses", "Dr", False),
    ("Miscellaneous Expenses", "Indirect Expenses", "Dr", False),
    ("Advertisement", "Indirect Expenses", "Dr", False),
]

LEDGER_RENAMES = {
    "Commission / Dalali Income": "Commission Account",
    "Audit Fees": "Audit Fee",
    "Hamali Income": "Hamali",
    "OD Interest": "Od Intrest",
    "Od Interest": "Od Intrest",
}

LEGACY_INCOME_GROUPS = ("Commission Income", "Other Income")
LEGACY_EXPENSE_GROUPS = (
    "Rent & Rates",
    "Electricity & Water",
    "Salary & Wages",
    "Office Expenses",
    "Communication",
    "Printing & Stationery",
    "Repairs & Maintenance",
    "Transportation",
    "Miscellaneous",
    "Expenses",
)


def ensure_default_ledgers(log=None):
    """
    Create missing default ledger groups and accounts.
    Never deletes existing records.
    Returns dict with counts: groups_created, accounts_created, groups_updated, accounts_updated.
    """
    stats = {
        "groups_created": 0,
        "accounts_created": 0,
        "groups_updated": 0,
        "accounts_updated": 0,
    }

    def _log(msg):
        if log:
            log(msg)

    for old_name, new_name in LEDGER_RENAMES.items():
        if LedgerAccount.objects.filter(name=new_name).exists():
            continue
        updated = LedgerAccount.objects.filter(name=old_name).update(name=new_name)
        if updated:
            _log(f"Renamed ledger: {old_name} -> {new_name}")

    group_map = {}
    for name, nature in DEFAULT_GROUPS:
        grp, created = LedgerGroup.objects.get_or_create(
            name=name, defaults={"nature": nature}
        )
        if created:
            stats["groups_created"] += 1
            _log(f"Group: {name} ({nature})")
        elif grp.nature != nature:
            grp.nature = nature
            grp.save(update_fields=["nature"])
            stats["groups_updated"] += 1
        group_map[name] = grp

    direct_incomes = group_map.get("Direct Incomes")
    indirect_expenses = group_map.get("Indirect Expenses")

    if direct_incomes:
        moved = LedgerAccount.objects.filter(
            group__name__in=LEGACY_INCOME_GROUPS
        ).update(group=direct_incomes)
        if moved:
            _log(f"Moved {moved} income ledger(s) -> Direct Incomes")

    if indirect_expenses:
        moved = LedgerAccount.objects.filter(
            group__name__in=LEGACY_EXPENSE_GROUPS
        ).exclude(group=indirect_expenses).update(group=indirect_expenses)
        if moved:
            _log(f"Moved {moved} expense ledger(s) -> Indirect Expenses")

    for name, group_name, balance_type, is_system in DEFAULT_ACCOUNTS:
        grp = group_map.get(group_name)
        if not grp:
            _log(f"Skipping {name}: group '{group_name}' not found")
            continue
        acc, created = LedgerAccount.objects.get_or_create(
            name=name,
            defaults={
                "group": grp,
                "balance_type": balance_type,
                "is_system": is_system,
                "opening_balance": 0,
            },
        )
        if created:
            stats["accounts_created"] += 1
            _log(f"Account: {name} ({group_name})")
        else:
            updated_fields = []
            if acc.group_id != grp.id:
                acc.group = grp
                updated_fields.append("group")
            if acc.is_system != is_system and is_system:
                acc.is_system = True
                updated_fields.append("is_system")
            if updated_fields:
                acc.save(update_fields=updated_fields)
                stats["accounts_updated"] += 1
                _log(f"Updated {name}: {', '.join(updated_fields)}")

    provision = group_map.get("Provision(Payable)")
    if provision:
        LedgerAccount.objects.filter(name="Cess Income").exclude(group=provision).update(
            group=provision
        )

    bank_accounts_grp = group_map.get("Bank Accounts")
    cash_bank_grp = group_map.get("Cash & Bank")
    if bank_accounts_grp and cash_bank_grp:
        LedgerAccount.objects.filter(group=cash_bank_grp).update(group=bank_accounts_grp)

    return stats
