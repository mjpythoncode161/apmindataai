from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0034_avak_rate"),
    ]

    operations = [
        migrations.CreateModel(
            name="CompanyProfile",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "company_name",
                    models.CharField(
                        blank=True, default="M S B AND COMPANY", max_length=200
                    ),
                ),
                (
                    "company_name_kannada",
                    models.CharField(
                        blank=True, default="ಎಂ ಎಸ್ ಬಿ & ಕಂಪನಿ", max_length=200
                    ),
                ),
                (
                    "address",
                    models.CharField(
                        blank=True,
                        default="APMC Yard, Byadgi – 581106",
                        max_length=500,
                    ),
                ),
                (
                    "gst_number",
                    models.CharField(
                        blank=True, default="29CFIPB5465B1ZL", max_length=20
                    ),
                ),
                ("phone", models.CharField(blank=True, default="", max_length=30)),
                (
                    "system_label",
                    models.CharField(blank=True, default="MSBC-2025-26", max_length=50),
                ),
                (
                    "logo",
                    models.ImageField(blank=True, null=True, upload_to="company/"),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Company Profile",
                "verbose_name_plural": "Company Profile",
            },
        ),
    ]
