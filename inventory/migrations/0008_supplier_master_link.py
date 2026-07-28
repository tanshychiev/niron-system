from django.db import migrations, models
import django.db.models.deletion


def link_existing_suppliers(apps, schema_editor):
    InventoryBatch = apps.get_model("inventory", "InventoryBatch")
    ProductionSupplier = apps.get_model("production", "ProductionSupplier")
    for batch in InventoryBatch.objects.filter(supplier_ref__isnull=True).exclude(supplier=""):
        name = (batch.supplier or "").strip()
        if not name:
            continue
        supplier = ProductionSupplier.objects.filter(name__iexact=name).first()
        if supplier is None:
            supplier = ProductionSupplier.objects.create(name=name, is_active=True)
        batch.supplier_ref_id = supplier.pk
        batch.save(update_fields=["supplier_ref"])


class Migration(migrations.Migration):
    dependencies = [
        ("production", "0003_suppliers_partner_expenses"),
        ("inventory", "0007_stockledger"),
    ]

    operations = [
        migrations.AddField(
            model_name="inventorybatch",
            name="supplier_ref",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="inventory_batches",
                to="production.productionsupplier",
            ),
        ),
        migrations.RunPython(link_existing_suppliers, migrations.RunPython.noop),
    ]
