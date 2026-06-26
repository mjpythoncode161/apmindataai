from django.db import models
from django.utils import timezone



class Users(models.Model):
    full_name = models.CharField(max_length=200)
    email = models.CharField(max_length=200)
    password = models.TextField()
    type = models.IntegerField(
        default=2
    )  # 1=Admin, 2=Customer, 3=Account, 4=Manager, 5=Lead
    contact = models.CharField(max_length=200, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, blank=True, null=True)

    class Meta:
        db_table = "users"

    def __str__(self):
        return self.full_name


class Farmer(models.Model):
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    name_kannada = models.CharField(max_length=200, blank=True, null=True)
    address_kannada = models.TextField(blank=True, null=True)

    # Bank Details
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_type = models.CharField(
        max_length=2, choices=[("Cr", "Cr"), ("Dr", "Dr")], default="Cr"
    )
    ifsc = models.CharField(max_length=20, blank=True, null=True)
    account_no = models.CharField(max_length=50, blank=True, null=True)
    bank_name = models.CharField(max_length=200, blank=True, null=True)
    branch_name = models.CharField(max_length=200, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Trader(models.Model):
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    name_kannada = models.CharField(max_length=200, blank=True, null=True)
    address_kannada = models.TextField(blank=True, null=True)

    # Bank Details
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_type = models.CharField(
        max_length=2, choices=[("Cr", "Cr"), ("Dr", "Dr")], default="Cr"
    )
    ifsc = models.CharField(max_length=20, blank=True, null=True)
    account_no = models.CharField(max_length=50, blank=True, null=True)
    bank_name = models.CharField(max_length=200, blank=True, null=True)
    branch_name = models.CharField(max_length=200, blank=True, null=True)

    # Trader specific additional fields
    short_code = models.CharField(max_length=50, blank=True, null=True)
    pan = models.CharField(max_length=15, blank=True, null=True)
    gstin = models.CharField(max_length=15, blank=True, null=True)
    mobile_no = models.CharField(max_length=15, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    pin = models.CharField(max_length=10, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Avak(models.Model):
    date = models.DateField()
    farmer = models.ForeignKey(Farmer, on_delete=models.CASCADE)
    place = models.CharField(max_length=255, blank=True, null=True)
    lot_number = models.CharField(max_length=100)
    variety = models.CharField(max_length=100, blank=True, null=True)
    no_of_bags = models.IntegerField(default=0)
    hamali_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    hamali_total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # New extra details fields
    freight = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    empty_bags = models.IntegerField(default=0)
    advance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Tender/sale rate per quintal",
    )
    buyer = models.ForeignKey(
        Trader,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="avak_purchases",
    )

    is_cancelled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["date", "lot_number"], name="unique_lot_per_day"
            )
        ]

    def __str__(self):
        return f"Avak {self.lot_number} - {self.farmer.name}"


class Bikri(models.Model):
    date = models.DateField()
    avak = models.ForeignKey(
        Avak, on_delete=models.CASCADE, related_name="bikri_entries"
    )
    buyer = models.ForeignKey(
        Trader, on_delete=models.CASCADE, related_name="bikri_purchases"
    )
    no_of_bags = models.IntegerField(default=0)
    rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )  # Rate per quintal
    total_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Trader calculations
    hamali = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    packing = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    dalali = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cess = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    weighman_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gst = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Store rates for reference in bills
    hamali_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    packing_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    dalali_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )  # percent
    cess_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )  # percent
    gst_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )  # percent

    # Farmer calculations (Image 4)
    farmer_hamali = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    farmer_packing = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    farmer_hamali_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    farmer_packing_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    farmer_unloading_rate = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    rent = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unload_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    other_fee_1 = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    other_fee_2 = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cash_deduct = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net_payable = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Added for transaction-specific farmer overrides
    farmer_name_override = models.CharField(max_length=255, null=True, blank=True)
    village_override = models.CharField(max_length=255, null=True, blank=True)

    bill_no = models.CharField(max_length=100, blank=True, null=True)

    is_cancelled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def amount_after_farmer_hamali(self):
        return self.amount - self.farmer_hamali

    @property
    def farmer_bill_amount(self):
        return self.amount - self.farmer_hamali + self.farmer_packing

    @property
    def other_deductions_total(self):
        return self.rent + self.unload_fee + self.other_fee_1 + self.other_fee_2

    def __str__(self):
        return f"Bikri {self.id} - Lot {self.avak.lot_number} - {self.buyer.name}"


class BikriBagWeight(models.Model):
    bikri = models.ForeignKey(Bikri, on_delete=models.CASCADE, related_name="weights")
    bag_no = models.IntegerField()
    weight = models.DecimalField(max_digits=6, decimal_places=2)

    def __str__(self):
        return f"Bikri {self.bikri_id} - Bag {self.bag_no}: {self.weight}"


class MarketRate(models.Model):
    date = models.DateField(unique=True)
    # Farmer Bills (ರೈತರ ಪಟ್ಟಿಗಳಲ್ಲಿ)
    farmer_packing_per_bag = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    farmer_hamali_per_bag = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    farmer_unloading_per_bag = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )

    # Purchase Bills (ಖರೀದಿ ಪಟ್ಟಿಗಳಲ್ಲಿ)
    trader_packing_per_bag = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    trader_hamali_per_bag = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    weighman_fee_per_bag = models.DecimalField(
        max_digits=10, decimal_places=2, default=0
    )
    dalali_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cess_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    gst_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    rakham_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    # TDS on buyer commission (Sec 194H) — Kharidi Patti
    tds_percent = models.DecimalField(max_digits=5, decimal_places=2, default=2)

    # Flags
    print_farmer_weights = models.BooleanField(default=False)
    print_detailed_bikri_bill = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "market_rates"

    def __str__(self):
        return f"Market Rates for {self.date}"


class TraderBill(models.Model):
    invoice_no = models.CharField(max_length=50, unique=True)
    date = models.DateField()
    buyer = models.ForeignKey(Trader, on_delete=models.CASCADE, related_name="bills")
    total_bags = models.IntegerField(default=0)
    total_weight = models.DecimalField(max_digits=15, decimal_places=3, default=0)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    
    commission = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    packing = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    hamali = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    weighman_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cess = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    round_off = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Bill {self.invoice_no} - {self.buyer.name}"


class TraderBillItem(models.Model):
    bill = models.ForeignKey(TraderBill, on_delete=models.CASCADE, related_name="items")
    bikri = models.OneToOneField(Bikri, on_delete=models.CASCADE)

    def __str__(self):
        return f"Item in {self.bill.invoice_no} - Lot {self.bikri.avak.lot_number}"


class BagTransfer(models.Model):
    """
    Records ownership reassignment of bags between buyers.
    - Does NOT create new Avak or Bikri records.
    - Avak remains completely untouched.
    - Source Bikri bag count is reduced by this transfer.
    - Target buyer gets billing credit for these bags WITHOUT any lot number.
    """
    date = models.DateField()
    source_bikri = models.ForeignKey(
        Bikri, on_delete=models.CASCADE, related_name="transfers_out"
    )
    target_buyer = models.ForeignKey(
        Trader, on_delete=models.CASCADE, related_name="transfers_in"
    )
    no_of_bags = models.IntegerField(default=0)
    total_weight = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Billing fields (computed at transfer time using same rates as source_bikri)
    hamali = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    packing = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    dalali = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cess = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gst = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    weighman_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Rate references
    hamali_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    packing_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    dalali_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cess_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gst_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    weighman_fee_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return (
            f"Transfer on {self.date}: {self.no_of_bags} bags "
            f"from {self.source_bikri} → {self.target_buyer.name}"
        )


class BagTransferWeight(models.Model):
    transfer = models.ForeignKey(BagTransfer, on_delete=models.CASCADE, related_name="weights")
    bag_no = models.IntegerField()
    weight = models.DecimalField(max_digits=6, decimal_places=2)

    def __str__(self):
        return f"Transfer {self.transfer_id} - Bag {self.bag_no}: {self.weight}"


class FinancialTransaction(models.Model):
    TRANSACTION_TYPES = [
        ("Debit", "Debit"),
        ("Credit", "Credit"),
    ]
    PAYMENT_METHODS = [
        ("Cash", "Cash"),
        ("Cheque", "Cheque"),
        ("RTGS", "RTGS"),
        ("NEFT", "NEFT"),
        ("Others", "Others"),
    ]
    PERSON_TYPES = [
        ("Farmer", "Farmer"),
        ("Trader", "Trader"),
    ]

    date = models.DateField(default=timezone.now)
    transaction_type = models.CharField(
        max_length=10, choices=TRANSACTION_TYPES, default="Debit"
    )
    person_type = models.CharField(
        max_length=10, choices=PERSON_TYPES, null=True, blank=True
    )
    farmer = models.ForeignKey(
        Farmer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_transactions",
    )
    trader = models.ForeignKey(
        Trader,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="financial_transactions",
    )

    # Fields for search/reference
    name = models.CharField(max_length=255, blank=True, null=True)
    place = models.CharField(max_length=255, blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    bikri_bill_no = models.CharField(max_length=100, blank=True, null=True)

    payment_method = models.CharField(
        max_length=20, choices=PAYMENT_METHODS, default="Cash"
    )
    pay_from_ledger = models.ForeignKey(
        "LedgerAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_from",
    )
    debit_ledger = models.ForeignKey(
        "LedgerAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_debit",
    )
    voucher_type = models.CharField(max_length=30, blank=True, null=True)
    cheque_no = models.CharField(max_length=50, blank=True, null=True)
    cheque_bank_name = models.CharField(max_length=100, blank=True, null=True)
    narration = models.TextField(blank=True, null=True)  # remarks
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.transaction_type} - {self.amount} - {self.date}"


# ─────────────────────────────────────────────────────────────────────────────
# Tally-style Double-Entry Voucher System
# ─────────────────────────────────────────────────────────────────────────────

class LedgerGroup(models.Model):
    NATURE_CHOICES = [
        ("Assets", "Assets"),
        ("Liabilities", "Liabilities"),
        ("Income", "Income"),
        ("Expenses", "Expenses"),
    ]
    name = models.CharField(max_length=200, unique=True)
    nature = models.CharField(max_length=20, choices=NATURE_CHOICES)

    class Meta:
        ordering = ["nature", "name"]

    def __str__(self):
        return f"{self.name} ({self.nature})"


class LedgerAccount(models.Model):
    name = models.CharField(max_length=200, unique=True)
    group = models.ForeignKey(
        LedgerGroup, on_delete=models.PROTECT, related_name="ledger_accounts"
    )
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance_type = models.CharField(
        max_length=2, choices=[("Dr", "Dr"), ("Cr", "Cr")], default="Dr"
    )
    is_system = models.BooleanField(default=False)  # Cannot be deleted by users

    # Optional link to a party account
    farmer = models.OneToOneField(
        Farmer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_account",
    )
    trader = models.OneToOneField(
        Trader,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ledger_account",
    )

    class Meta:
        ordering = ["group__nature", "name"]

    def __str__(self):
        return self.name

    @property
    def group_nature(self):
        return self.group.nature


class Voucher(models.Model):
    VOUCHER_TYPES = [
        ("Payment", "Payment"),
        ("Receipt", "Receipt"),
        ("Journal", "Journal"),
        ("Contra", "Contra"),
    ]
    voucher_type = models.CharField(max_length=20, choices=VOUCHER_TYPES)
    voucher_no = models.CharField(max_length=50)
    date = models.DateField()
    narration = models.TextField(blank=True, null=True)
    bikri_bill_no = models.CharField(max_length=100, blank=True, null=True, verbose_name="Vikri Bill No")

    # Reference to source document (for auto-generated vouchers)
    ref_bikri = models.ForeignKey(
        Bikri,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vouchers",
    )
    ref_trader_bill = models.ForeignKey(
        'TraderBill',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="trader_vouchers",
    )
    is_auto = models.BooleanField(default=False)  # Auto-generated from bikri or trader bill

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.voucher_type} #{self.voucher_no} – {self.date}"

    @property
    def total_debit(self):
        from django.db.models import Sum
        return self.lines.filter(entry_type="Dr").aggregate(t=Sum("amount"))["t"] or 0

    @property
    def total_credit(self):
        from django.db.models import Sum
        return self.lines.filter(entry_type="Cr").aggregate(t=Sum("amount"))["t"] or 0


class VoucherLine(models.Model):
    ENTRY_TYPES = [("Dr", "Debit"), ("Cr", "Credit")]

    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name="lines")
    ledger = models.ForeignKey(
        LedgerAccount, on_delete=models.PROTECT, related_name="voucher_lines"
    )
    entry_type = models.CharField(max_length=2, choices=ENTRY_TYPES)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    narration = models.CharField(max_length=500, blank=True, null=True)

    def __str__(self):
        return f"{self.ledger.name}  {self.entry_type}  {self.amount}"


class BankMaster(models.Model):
    """Single company bank details for invoices (pk=1)."""
    bank_name = models.CharField(max_length=200, blank=True, default="")
    account_holder = models.CharField(max_length=200, blank=True, default="")
    account_number = models.CharField(max_length=50, blank=True, default="")
    ifsc_code = models.CharField(max_length=20, blank=True, default="")
    branch = models.CharField(max_length=200, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Bank Master"
        verbose_name_plural = "Bank Master"

    @classmethod
    def get_settings(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def display_lines(self):
        lines = []
        if self.bank_name:
            lines.append(f"Bank : {self.bank_name}")
        if self.account_number:
            lines.append(f"A/c No : {self.account_number}")
        if self.ifsc_code:
            lines.append(f"IFSC : {self.ifsc_code}")
        if self.branch:
            lines.append(f"Branch : {self.branch}")
        return lines


class CompanyProfile(models.Model):
    """Single company profile for invoices, reports, and headers (pk=1)."""
    company_name = models.CharField(
        max_length=200, blank=True, default="M S B AND COMPANY"
    )
    company_name_kannada = models.CharField(
        max_length=200, blank=True, default="ಎಂ ಎಸ್ ಬಿ & ಕಂಪನಿ"
    )
    address = models.CharField(
        max_length=500, blank=True, default="APMC Yard, Byadgi – 581106"
    )
    gst_number = models.CharField(max_length=20, blank=True, default="29CFIPB5465B1ZL")
    phone = models.CharField(max_length=30, blank=True, default="")
    system_label = models.CharField(max_length=50, blank=True, default="MSBC-2025-26")
    logo = models.ImageField(upload_to="company/", blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Company Profile"
        verbose_name_plural = "Company Profile"

    @classmethod
    def get_settings(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def display_name(self):
        return (self.company_name_kannada or self.company_name or "").strip()

    @property
    def display_name_english(self):
        return (self.company_name or self.company_name_kannada or "").strip()

    @property
    def display_address(self):
        return (self.address or "").strip()

    def header_subtitle(self):
        parts = [p for p in [self.display_address, self.gst_number] if p]
        return " | ".join(parts)

