from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.contrib.auth.models import User

from accounts.models import (
    Users,
    Farmer,
    Trader,
    MarketRate,
    Avak,
    Bikri,
    BikriBagWeight,
    TraderBill,
    TraderBillItem,
    BagTransfer,
    BagTransferWeight,
    FinancialTransaction,
    Voucher,
    VoucherLine,
)
from accounts.ledger_defaults import ensure_default_ledgers


class Command(BaseCommand):
    help = (
        "Delete records for a fresh start. "
        "Default: transaction data only. Use --all to wipe farmers/traders/users "
        "(ledger groups and default accounts are always kept)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Delete farmers, traders, users, and transactions (keeps ledger master defaults).",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm and execute deletion.",
        )
        parser.add_argument(
            "--seed-admin",
            action="store_true",
            help="After --all, recreate admin@admin.com (password: 123).",
        )

    def _reset_sqlite_sequences(self, model_list):
        table_names = [m._meta.db_table for m in model_list]
        if not table_names:
            return

        with connection.cursor() as cursor:
            for table in table_names:
                try:
                    cursor.execute(
                        "DELETE FROM sqlite_sequence WHERE name = %s;",
                        [table],
                    )
                except Exception:
                    pass

    def _seed_admin(self):
        if not User.objects.filter(username="admin@admin.com").exists():
            User.objects.create_superuser("admin@admin.com", "admin@admin.com", "123")
            self.stdout.write("  Created Django superuser admin@admin.com / 123")
        if not Users.objects.filter(email="admin@admin.com").exists():
            Users.objects.create(
                full_name="Admin User",
                contact="9999999999",
                email="admin@admin.com",
                password="123",
                type=1,
            )
            self.stdout.write("  Created Users record for admin@admin.com")

    def handle(self, *args, **options):
        delete_all = options.get("all", False)
        confirmed = options.get("yes", False)
        seed_admin = options.get("seed_admin", False)

        if not confirmed:
            self.stdout.write(self.style.WARNING("No changes made."))
            self.stdout.write(
                "Examples:\n"
                "  python manage.py truncate_records --yes\n"
                "  python manage.py truncate_records --all --yes --seed-admin"
            )
            return

        # Child tables first (FK order)
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

        all_models = transaction_models + master_models

        with transaction.atomic():
            if delete_all:
                self.stdout.write(self.style.WARNING("Deleting ALL records (--all)..."))
                models_to_delete = all_models
            else:
                self.stdout.write("Deleting transaction records only...")
                models_to_delete = transaction_models

            for model in models_to_delete:
                deleted_count, _ = model.objects.all().delete()
                self.stdout.write(f"  {model.__name__}: {deleted_count} deleted")

            self._reset_sqlite_sequences(models_to_delete)

        ensure_default_ledgers()

        if delete_all and seed_admin:
            self.stdout.write("Seeding admin user...")
            self._seed_admin()

        if delete_all:
            self.stdout.write(self.style.SUCCESS("All records deleted. Database is empty."))
            self.stdout.write("Ledger groups and default accounts preserved / restored.")
            if not seed_admin:
                self.stdout.write(
                    self.style.WARNING(
                        "No login user left. Run with --seed-admin to recreate admin@admin.com / 123"
                    )
                )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "Transaction records deleted. Master data (farmers, traders, ledgers) kept."
                )
            )
