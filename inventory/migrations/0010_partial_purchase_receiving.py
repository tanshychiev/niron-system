from decimal import Decimal
from django.db import migrations, models
from django.core.validators import MinValueValidator


def backfill_arrived(apps, schema_editor):
    Batch = apps.get_model("inventory", "InventoryBatch")
    Item = apps.get_model("inventory", "InventoryBatchItem")
    for row in Item.objects.select_related("batch").all().iterator():
        if row.batch.status == "RECEIVED":
            row.qty_arrived = row.qty_received
        else:
            # Old waiting purchases had qty_remaining=0. Keep any unusual existing stock safe.
            row.qty_arrived = max(row.qty_remaining or Decimal("0"), Decimal("0"))
        row.save(update_fields=["qty_arrived"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0009_inventorybatch_cost_added_at_and_more")]

    operations = [
        migrations.AddField(
            model_name="inventorybatchitem",
            name="qty_arrived",
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=12,
                validators=[MinValueValidator(Decimal("0"))],
            ),
        ),
        migrations.AlterField(
            model_name="inventorybatch",
            name="status",
            field=models.CharField(
                choices=[
                    ("COMING_SOON", "Waiting Arrival"),
                    ("PARTIAL", "Partially Received"),
                    ("RECEIVED", "Fully Received"),
                ],
                default="RECEIVED",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_arrived, migrations.RunPython.noop),
    ]
