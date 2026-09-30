from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0010_partial_purchase_receiving"),
    ]

    operations = [
        migrations.AddField(
            model_name="size",
            name="product_type",
            field=models.CharField(
                choices=[("SHIRT", "Shirt"), ("TOTE_BAG", "Tote Bag")],
                default="SHIRT",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="inventoryitem",
            name="item_type",
            field=models.CharField(
                choices=[
                    ("SHIRT", "Shirt"),
                    ("TOTE_BAG", "Tote Bag"),
                    ("FILM", "Film"),
                    ("INK", "Ink"),
                    ("POWDER", "Powder"),
                    ("MAINTENANCE", "Maintenance"),
                    ("OTHER", "Other"),
                ],
                default="SHIRT",
                max_length=20,
            ),
        ),
    ]
