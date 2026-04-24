from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone


class Size(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=50)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.name


class Color(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=50)
    hex_code = models.CharField(max_length=7, default="#000000")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        value = (self.hex_code or "").strip().upper()
        if not value:
            value = "#000000"
        if not value.startswith("#"):
            value = f"#{value}"
        self.hex_code = value[:7]
        super().save(*args, **kwargs)


class InventoryItem(models.Model):
    # ===== TYPE =====
    TYPE_SHIRT = "SHIRT"
    TYPE_FILM = "FILM"
    TYPE_INK = "INK"
    TYPE_POWDER = "POWDER"
    TYPE_MAINTENANCE = "MAINTENANCE"
    TYPE_OTHER = "OTHER"

    TYPE_CHOICES = [
        (TYPE_SHIRT, "Shirt"),
        (TYPE_FILM, "Film"),
        (TYPE_INK, "Ink"),
        (TYPE_POWDER, "Powder"),
        (TYPE_MAINTENANCE, "Maintenance"),
        (TYPE_OTHER, "Other"),
    ]

    # ===== UNIT =====
    UNIT_PCS = "PCS"
    UNIT_METER = "METER"
    UNIT_ROLL = "ROLL"
    UNIT_BOTTLE = "BOTTLE"
    UNIT_PACK = "PACK"
    UNIT_KG = "KG"
    UNIT_ML = "ML"
    UNIT_LITER = "LITER"

    UNIT_CHOICES = [
        (UNIT_PCS, "PCS"),
        (UNIT_METER, "Meter"),
        (UNIT_ROLL, "Roll"),
        (UNIT_BOTTLE, "Bottle"),
        (UNIT_PACK, "Pack"),
        (UNIT_KG, "KG"),
        (UNIT_ML, "ML"),
        (UNIT_LITER, "Liter"),
    ]

    # ===== STYLE (ONLY FOR SHIRT) =====
    STYLE_OVERSIZE = "OVERSIZE"
    STYLE_POLO = "POLO"
    STYLE_BOXY = "BOXY"

    STYLE_CHOICES = [
        (STYLE_OVERSIZE, "Oversize"),
        (STYLE_POLO, "Polo"),
        (STYLE_BOXY, "Boxy"),
    ]

    # ===== BASIC =====
    code = models.CharField(max_length=50, unique=True, blank=True)
    name = models.CharField(max_length=150)

    item_type = models.CharField(
        max_length=20,
        choices=TYPE_CHOICES,
        default=TYPE_SHIRT,
    )

    unit = models.CharField(
        max_length=20,
        choices=UNIT_CHOICES,
        default=UNIT_PCS,
    )

    sample_style = models.CharField(
        max_length=20,
        choices=STYLE_CHOICES,
        default=STYLE_OVERSIZE,
        blank=True,
    )

    # 🔥 IMAGE (ONLY USED FOR NON-SHIRT)
    image = models.ImageField(
        upload_to="inventory/items/",
        blank=True,
        null=True,
    )

    is_active = models.BooleanField(default=True)
    note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code", "id"]

    def __str__(self):
        return f"{self.code} - {self.name}"

    # ===== AUTO CODE =====
    def save(self, *args, **kwargs):
        if not self.code:
            name_lower = (self.name or "").strip().lower()

            if "oversize" in name_lower:
                base = "OS"
            elif "boxy" in name_lower:
                base = "BX"
            elif "polo" in name_lower:
                base = "PO"
            elif "film" in name_lower:
                base = "FL"
            elif "ink" in name_lower:
                base = "INK"
            elif "powder" in name_lower:
                base = "PWD"
            elif "tube" in name_lower:
                base = "TUBE"
            elif "damper" in name_lower:
                base = "DMP"
            elif "motor" in name_lower:
                base = "MTR"
            else:
                base = "IT"

            code = base
            i = 1
            while InventoryItem.objects.filter(code=code).exclude(pk=self.pk).exists():
                code = f"{base}{i}"
                i += 1

            self.code = code

        # 👕 ONLY SHIRT uses style
        if self.item_type != self.TYPE_SHIRT:
            self.sample_style = ""

        super().save(*args, **kwargs)

    # ===== TOTAL STOCK =====
    @property
    def total_stock(self):
        result = self.batch_items.filter(
            is_active=True,
            batch__is_deleted=False,
        ).aggregate(total=Sum("qty_remaining"))
        return result["total"] or Decimal("0")
class InventoryBatch(models.Model):
    STATUS_DRAFT = "DRAFT"
    STATUS_FINAL = "FINAL"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_FINAL, "Final"),
    ]

    batch_no = models.CharField(max_length=50, unique=True)
    supplier = models.CharField(max_length=120, blank=True)
    received_date = models.DateField()
    total_goods_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    extra_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    note = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_batches_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_batches_updated",
    )
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_batches_deleted",
    )

    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_date", "-id"]

    def __str__(self):
        return self.batch_no

    @property
    def total_expense(self):
        return (self.total_goods_cost or 0) + (self.shipping_cost or 0) + (self.extra_cost or 0)

    @property
    def total_cloth(self):
        result = self.items.filter(
            is_active=True,
            item__item_type=InventoryItem.TYPE_SHIRT,
        ).aggregate(total=Sum("qty_received"))
        return result["total"] or Decimal("0")


class InventoryBatchItem(models.Model):
    batch = models.ForeignKey(
        InventoryBatch,
        on_delete=models.CASCADE,
        related_name="items",
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="batch_items",
    )
    color = models.ForeignKey(
        Color,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="batch_items",
    )
    size = models.ForeignKey(
        Size,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="batch_items",
    )

    qty_received = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    qty_remaining = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    base_unit_cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    final_unit_cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["batch__received_date", "id"]

    def __str__(self):
        parts = [self.item.code]
        if self.color:
            parts.append(self.color.name)
        if self.size:
            parts.append(self.size.name)
        return " / ".join(parts)

    @property
    def qty_used(self):
        return (self.qty_received or Decimal("0")) - (self.qty_remaining or Decimal("0"))

    @property
    def can_edit_received_qty(self):
        return self.qty_used == 0


class InventoryBatchHistory(models.Model):
    ACTION_CREATE = "CREATE"
    ACTION_UPDATE = "UPDATE"
    ACTION_DELETE = "DELETE"

    ACTION_CHOICES = [
        (ACTION_CREATE, "Create"),
        (ACTION_UPDATE, "Update"),
        (ACTION_DELETE, "Delete"),
    ]

    batch = models.ForeignKey(
        InventoryBatch,
        on_delete=models.CASCADE,
        related_name="history_logs",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_batch_history_logs",
    )
    changed_at = models.DateTimeField(default=timezone.now)
    note = models.TextField(blank=True, default="")
    snapshot_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-changed_at", "-id"]

    def __str__(self):
        return f"{self.batch.batch_no} - {self.action}"


class InventoryAdjustment(models.Model):
    TYPE_ADD = "ADD"
    TYPE_REMOVE = "REMOVE"
    TYPE_DAMAGE = "DAMAGE"
    TYPE_LOST = "LOST"
    TYPE_FOUND = "FOUND"
    TYPE_STOCKTAKE = "STOCKTAKE"

    TYPE_CHOICES = [
        (TYPE_ADD, "Add"),
        (TYPE_REMOVE, "Remove"),
        (TYPE_DAMAGE, "Damage"),
        (TYPE_LOST, "Lost"),
        (TYPE_FOUND, "Found Extra"),
        (TYPE_STOCKTAKE, "Stock Take"),
    ]

    batch_item = models.ForeignKey(
        InventoryBatchItem,
        on_delete=models.PROTECT,
        related_name="adjustments",
    )
    adjustment_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    qty = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_adjustments_created",
    )
    qty_before = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    qty_after = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.batch_item_id} - {self.adjustment_type} - {self.qty}"

    def clean(self):
        if self.qty is not None and self.qty <= 0:
            raise ValidationError("Adjustment qty must be greater than 0.")