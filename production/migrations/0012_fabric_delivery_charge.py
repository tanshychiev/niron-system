from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0011_borib_stock_and_usage"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FabricDeliveryCharge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("charge_date", models.DateField(default=django.utils.timezone.localdate)),
                ("amount", models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=14, validators=[MinValueValidator(Decimal("0"))])),
                ("allocation_method", models.CharField(choices=[("KG", "By KG"), ("ROLLS", "By Rolls"), ("EQUAL", "Equal"), ("MANUAL", "Manual")], default="KG", max_length=12)),
                ("delivery_company", models.CharField(blank=True, default="", max_length=150)),
                ("note", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fabric_delivery_charges_created", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-charge_date", "-id"]},
        ),
        migrations.CreateModel(
            name="FabricDeliveryAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("purchase_group", models.CharField(db_index=True, max_length=60)),
                ("allocated_amount", models.DecimalField(decimal_places=2, default=Decimal("0"), max_digits=14)),
                ("batch_kg", models.DecimalField(decimal_places=3, default=Decimal("0"), max_digits=14)),
                ("batch_rolls", models.PositiveIntegerField(default=0)),
                ("anchor_receipt", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="delivery_allocations", to="production.fabricreceipt")),
                ("charge", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="allocations", to="production.fabricdeliverycharge")),
            ],
            options={"ordering": ["charge_id", "id"]},
        ),
        migrations.AddConstraint(
            model_name="fabricdeliveryallocation",
            constraint=models.UniqueConstraint(fields=("charge", "purchase_group"), name="unique_fabric_delivery_charge_group"),
        ),
    ]
