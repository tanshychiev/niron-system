from decimal import Decimal
from django.db import migrations, models
import django.db.models.deletion
import django.core.validators
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("production", "0010_fabric_purchase_receive_flow")]
    operations = [
        migrations.AddField(
            model_name="fabricreceipt", name="borib_kg",
            field=models.DecimalField(decimal_places=3, default=Decimal("0"), max_digits=10, validators=[django.core.validators.MinValueValidator(Decimal("0"))]),
        ),
        migrations.CreateModel(
            name="BoribStock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity_kg", models.DecimalField(decimal_places=3, default=Decimal("0"), max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal("0"))])),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("color", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="borib_stock", to="inventory.color")),
            ], options={"ordering": ["color__name"]},
        ),
        migrations.CreateModel(
            name="CuttingBoribUsage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity_kg", models.DecimalField(decimal_places=3, default=Decimal("0"), max_digits=10, validators=[django.core.validators.MinValueValidator(Decimal("0"))])),
                ("stock_qty_before", models.DecimalField(decimal_places=3, default=Decimal("0"), max_digits=12)),
                ("stock_qty_after", models.DecimalField(decimal_places=3, default=Decimal("0"), max_digits=12)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="borib_usages", to="production.productionproject")),
                ("project_color", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="borib_usage", to="production.productionprojectcolor")),
            ], options={"ordering": ["id"]},
        ),
    ]
