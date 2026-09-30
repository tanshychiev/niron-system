from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_userprofile_is_staff_employee_userprofile_join_date_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="staff_position",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="staffpayroll",
            name="deduction_reason",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="staffpayroll",
            name="finance_expense_id",
            field=models.PositiveIntegerField(blank=True, editable=False, null=True),
        ),
    ]
