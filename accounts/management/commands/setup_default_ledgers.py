"""
Management command to create default Ledger Groups and Ledger Accounts
for the Tally Voucher System.

Usage:
    python manage.py setup_default_ledgers

Safe to run anytime — only adds missing defaults, does not delete your data.
Use --reset only if you intentionally want to wipe all ledger data first.
"""

from django.core.management.base import BaseCommand

from accounts.models import LedgerAccount, LedgerGroup
from accounts.ledger_defaults import DEFAULT_BANK_ACCOUNTS, ensure_default_ledgers


class Command(BaseCommand):
    help = "Create default ledger groups and accounts (never deletes unless --reset)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete ALL ledger groups/accounts before creating defaults (DANGEROUS)",
        )

    def handle(self, *args, **options):
        if options["reset"]:
            self.stdout.write(self.style.WARNING("Deleting all existing ledger data..."))
            LedgerAccount.objects.all().delete()
            LedgerGroup.objects.all().delete()

        stats = ensure_default_ledgers(log=self.stdout.write)

        self.stdout.write(self.style.SUCCESS("\nDefault ledger groups and accounts ready."))
        self.stdout.write(
            f"Groups created: {stats['groups_created']}, "
            f"accounts created: {stats['accounts_created']}"
        )
        self.stdout.write(
            f"Bank Accounts: Cash in Hand + {len(DEFAULT_BANK_ACCOUNTS)} banks."
        )
        self.stdout.write("Direct Incomes: Commission, Hamali, Packing, etc.")
        self.stdout.write("Provision(Payable): Cess, Output SGST/CGST, GST Payable.")
        self.stdout.write("Indirect Expenses: Rent, Salaries, Bank Charges, etc.")
