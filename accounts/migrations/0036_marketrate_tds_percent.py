from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0035_companyprofile"),
    ]

    operations = [
        migrations.AddField(
            model_name="marketrate",
            name="tds_percent",
            field=models.DecimalField(decimal_places=2, default=2, max_digits=5),
        ),
    ]
