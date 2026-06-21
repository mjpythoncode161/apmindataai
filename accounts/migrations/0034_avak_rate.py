from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0033_bankmaster"),
    ]

    operations = [
        migrations.AddField(
            model_name="avak",
            name="rate",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text="Tender/sale rate per quintal",
                max_digits=10,
            ),
        ),
    ]
