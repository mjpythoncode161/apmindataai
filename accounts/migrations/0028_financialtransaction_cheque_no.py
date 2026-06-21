from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0027_financialtransaction_debit_ledger_voucher_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='financialtransaction',
            name='cheque_no',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
