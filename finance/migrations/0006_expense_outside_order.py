from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [("finance", "0005_expense_expense_date"), ("orders", "0027_order_purchase_options")]
    operations = [
        migrations.AddField(model_name="expense", name="outside_order", field=models.OneToOneField(to="orders.order", null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, related_name="outside_purchase_expense")),
    ]
