import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0026_farmer_payment_ledger'),
    ]

    operations = [
        migrations.AddField(
            model_name='financialtransaction',
            name='debit_ledger',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='payments_debit',
                to='accounts.ledgeraccount',
            ),
        ),
        migrations.AddField(
            model_name='financialtransaction',
            name='voucher_type',
            field=models.CharField(blank=True, max_length=30, null=True),
        ),
    ]
