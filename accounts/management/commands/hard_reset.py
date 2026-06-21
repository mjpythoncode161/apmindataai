import os
from datetime import date
from decimal import Decimal
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection
from django.contrib.auth.models import User
from accounts.models import Users, Farmer, Trader, MarketRate, Avak, Bikri, BikriBagWeight, TraderBill, TraderBillItem

class Command(BaseCommand):
    help = 'Performs a hard reset of the database, re-runs migrations, and leaves only the admin@admin.com user with password 123.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Starting hard reset..."))
        
        db_path = settings.DATABASES['default']['NAME']
        
        # 1. Try to delete the database file to start fresh
        db_deleted = False
        try:
            connection.close()
            if os.path.exists(db_path):
                os.remove(db_path)
                self.stdout.write(self.style.SUCCESS(f"Successfully deleted database file: {db_path}"))
                db_deleted = True
        except PermissionError:
            self.stdout.write(self.style.WARNING(
                "Database file is locked (possibly by runserver). Falling back to clearing all tables..."
            ))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Could not delete database file: {e}. Falling back to clearing all tables..."))
            
        if db_deleted:
            # Re-run migrations to create the database schema
            self.stdout.write("Running migrations...")
            call_command('migrate', interactive=False)
            self.stdout.write(self.style.SUCCESS("Migrations completed successfully."))
        else:
            # Fallback: Delete all objects from all tables
            self.stdout.write("Clearing all data from tables...")
            TraderBillItem.objects.all().delete()
            TraderBill.objects.all().delete()
            BikriBagWeight.objects.all().delete()
            Bikri.objects.all().delete()
            Avak.objects.all().delete()
            MarketRate.objects.all().delete()
            Trader.objects.all().delete()
            Farmer.objects.all().delete()
            Users.objects.all().delete()
            User.objects.all().delete()
            
            # Reset SQLite auto-increment counters
            with connection.cursor() as cursor:
                try:
                    cursor.execute("DELETE FROM sqlite_sequence;")
                    self.stdout.write(self.style.SUCCESS("Successfully reset all auto-increment counters."))
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"Could not reset auto-increment counters: {e}"))
            
            self.stdout.write(self.style.SUCCESS("Successfully cleared all data."))

        # 2. Seed only the requested admin user
        self.stdout.write("Seeding only the admin@admin.com user...")
        
        # Create superuser in the Django auth User model
        if not User.objects.filter(username='admin@admin.com').exists():
            User.objects.create_superuser('admin@admin.com', 'admin@admin.com', '123')
            self.stdout.write("Created Django superuser 'admin@admin.com' with password '123'")
            
        # Create corresponding entry in the custom Users model
        if not Users.objects.filter(email='admin@admin.com').exists():
            Users.objects.create(
                full_name='Admin User',
                contact='9999999999',
                email='admin@admin.com',
                password='123',
                type=1  # 1=Admin
            )
            self.stdout.write("Created custom Users record for 'admin@admin.com'")

        self.stdout.write(self.style.SUCCESS("Hard reset and truncation completed successfully!"))

