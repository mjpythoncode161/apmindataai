from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0032_voucher_ref_trader_bill"),
    ]

    operations = [
        migrations.CreateModel(
            name="BankMaster",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("bank_name", models.CharField(blank=True, default="", max_length=200)),
                ("account_holder", models.CharField(blank=True, default="", max_length=200)),
                ("account_number", models.CharField(blank=True, default="", max_length=50)),
                ("ifsc_code", models.CharField(blank=True, default="", max_length=20)),
                ("branch", models.CharField(blank=True, default="", max_length=200)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Bank Master",
                "verbose_name_plural": "Bank Master",
            },
        ),
    ]
