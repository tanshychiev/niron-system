from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0011_inventoryitem_tote_bag_size_product_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="inventoryitem",
            name="sample_style",
            field=models.CharField(blank=True, default="OVERSIZE", max_length=60),
        ),
    ]
