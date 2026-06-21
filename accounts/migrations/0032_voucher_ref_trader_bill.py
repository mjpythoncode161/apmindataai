from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0031_bikri_bill_no'),
    ]

    operations = [
        migrations.AddField(
            model_name='voucher',
            name='ref_trader_bill',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='trader_vouchers',
                to='accounts.traderbill',
            ),
        ),
    ]
