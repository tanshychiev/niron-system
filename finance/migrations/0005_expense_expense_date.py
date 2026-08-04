from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0004_expense_expense_status_expense_fabric_receipt_ids_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="expense",
            name="expense_date",
            field=models.DateField(default=django.utils.timezone.localdate),
        ),
    ]
