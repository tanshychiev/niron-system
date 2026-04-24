from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from inventory.models import Color, InventoryBatchItem, InventoryItem, Size


class Order(models.Model):
    STATUS_PENDING = "PENDING"
    STATUS_PROCESSING = "PROCESSING"
    STATUS_DONE = "DONE"
    STATUS_CANCEL = "CANCEL"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_DONE, "Done"),
        (STATUS_CANCEL, "Cancel"),
    ]

    TYPE_NIRON = "NIRON"
    TYPE_KAMPU = "KAMPU"

    TYPE_CHOICES = [
        (TYPE_NIRON, "Niron"),
        (TYPE_KAMPU, "Kampu"),
    ]

    SERVICE_FULL = "FULL"
    SERVICE_FILM_ONLY = "FILM_ONLY"
    SERVICE_PRINT_HEATPRESS = "PRINT_HEATPRESS"

    SERVICE_CHOICES = [
        (SERVICE_FULL, "Full Order"),
        (SERVICE_FILM_ONLY, "Film Only"),
        (SERVICE_PRINT_HEATPRESS, "Print & Heat Press"),
    ]

    # ===== BASIC =====
    order_no = models.CharField(max_length=50, unique=True, blank=True)
    order_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_NIRON)
    service_type = models.CharField(max_length=30, choices=SERVICE_CHOICES, default=SERVICE_FULL)

    customer_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30, blank=True, default="")
    customer_location = models.CharField(max_length=255, blank=True, default="")
    deadline = models.DateTimeField()

    # ✅ NEW (created by)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_orders",
    )

    # ===== MONEY =====
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    deposit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # ===== STATUS =====
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    remark = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    # ===== STOCK =====
    stock_deducted = models.BooleanField(default=False)

    # ===== PRODUCTION =====
    total_pcs = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    done_pcs = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # ===== TRASH SYSTEM =====
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    deleted_reason = models.TextField(blank=True, default="")
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deleted_orders",
    )

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return self.order_no or f"Order {self.pk}"

    @property
    def balance_amount(self):
        return (self.total_amount or Decimal("0")) - (self.paid_amount or Decimal("0"))

    @property
    def remaining_pcs(self):
        return (self.total_pcs or Decimal("0")) - (self.done_pcs or Decimal("0"))

    def save(self, *args, **kwargs):
        if not self.order_no:
            last_id = (
                Order.objects.order_by("-id").values_list("id", flat=True).first() or 0
            ) + 1
            self.order_no = f"NR-{last_id:06d}"
        super().save(*args, **kwargs)
class OrderDesign(models.Model):
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="designs",
    )
    name = models.CharField(max_length=120, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=1)
    remark = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"{self.order.order_no} - {self.display_name}"

    @property
    def display_name(self):
        return self.name.strip() if self.name else f"Design {self.sort_order}"

    @property
    def total_pcs(self):
        total = Decimal("0")
        for item in self.items.all():
            total += item.quantity or Decimal("0")
        return total

    @property
    def done_pcs(self):
        total = Decimal("0")
        for item in self.items.all():
            total += item.done_qty or Decimal("0")
        return total

    @property
    def remaining_pcs(self):
        return self.total_pcs - self.done_pcs

    @property
    def total_amount(self):
        total = Decimal("0")
        for item in self.items.all():
            total += item.line_total or Decimal("0")
        return total


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
    )
    design = models.ForeignKey(
        OrderDesign,
        on_delete=models.CASCADE,
        related_name="items",
        null=True,
        blank=True,
    )

    description = models.CharField(max_length=200, blank=True, default="")

    shirt_item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="order_shirt_items",
        blank=True,
        null=True,
    )
    color = models.ForeignKey(
        Color,
        on_delete=models.PROTECT,
        related_name="order_items",
        blank=True,
        null=True,
    )
    size = models.ForeignKey(
        Size,
        on_delete=models.PROTECT,
        related_name="order_items",
        blank=True,
        null=True,
    )

    film_item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="order_film_items",
        blank=True,
        null=True,
    )
    film_meter = models.DecimalField(max_digits=12, decimal_places=4, default=0)

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        default=1,
    )
    done_qty = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        base_name = self.description or (
            str(self.shirt_item)
            if self.shirt_item
            else str(self.film_item)
            if self.film_item
            else f"Item {self.pk}"
        )
        return f"{self.order.order_no} - {base_name}"

    def clean(self):
        if self.design_id:
            self.order = self.design.order

        if not self.order_id:
            return

        service_type = self.order.service_type

        if service_type == Order.SERVICE_FULL:
            if not self.shirt_item:
                raise ValidationError({"shirt_item": "Full Order requires shirt item."})
            if not self.color:
                raise ValidationError({"color": "Full Order requires color."})
            if not self.size:
                raise ValidationError({"size": "Full Order requires size."})

            self.film_item = None
            self.film_meter = Decimal("0")

        elif service_type == Order.SERVICE_FILM_ONLY:
            if not self.film_item:
                raise ValidationError({"film_item": "Film Only requires film item."})
            if not self.film_meter or self.film_meter <= 0:
                raise ValidationError({"film_meter": "Film Only requires film meter."})

            self.shirt_item = None
            self.color = None
            self.size = None

        elif service_type == Order.SERVICE_PRINT_HEATPRESS:
            self.shirt_item = None
            self.color = None
            self.size = None
            self.film_item = None
            self.film_meter = Decimal("0")

    def save(self, *args, **kwargs):
        if self.design_id:
            self.order = self.design.order

        self.line_total = (self.quantity or Decimal("0")) * (self.unit_price or Decimal("0"))
        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def remaining_qty(self):
        return (self.quantity or Decimal("0")) - (self.done_qty or Decimal("0"))


class StockConsumption(models.Model):
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="stock_consumptions",
    )
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.CASCADE,
        related_name="stock_consumptions",
    )
    batch_item = models.ForeignKey(
        InventoryBatchItem,
        on_delete=models.PROTECT,
        related_name="stock_consumptions",
    )
    consumed_qty = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.order.order_no} - {self.batch_item} - {self.consumed_qty}"


class OrderDesignFile(models.Model):
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="design_files",
    )
    design = models.ForeignKey(
        OrderDesign,
        on_delete=models.CASCADE,
        related_name="files",
        null=True,
        blank=True,
    )
    image = models.ImageField(upload_to="order_designs/")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        if self.design_id:
            return f"{self.order.order_no} - {self.design.display_name}"
        return f"{self.order.order_no} - Design {self.pk}"

    def save(self, *args, **kwargs):
        if self.design_id:
            self.order = self.design.order
        super().save(*args, **kwargs)


class OrderProgress(models.Model):
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="progress_logs",
    )
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.CASCADE,
        related_name="progress_logs",
        null=True,
        blank=True,
    )
    qty_done = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    remark = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.order.order_no} +{self.qty_done}"


class OrderHistory(models.Model):
    ACTION_CREATE = "CREATE"
    ACTION_EDIT = "EDIT"
    ACTION_ITEM_ADD = "ITEM_ADD"
    ACTION_ITEM_EDIT = "ITEM_EDIT"
    ACTION_ITEM_DELETE = "ITEM_DELETE"
    ACTION_DESIGN_ADD = "DESIGN_ADD"

    ACTION_CHOICES = [
        (ACTION_CREATE, "Create"),
        (ACTION_EDIT, "Edit"),
        (ACTION_ITEM_ADD, "Item Add"),
        (ACTION_ITEM_EDIT, "Item Edit"),
        (ACTION_ITEM_DELETE, "Item Delete"),
        (ACTION_DESIGN_ADD, "Design Add"),
    ]

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="history_logs",
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, default=ACTION_EDIT)
    field_name = models.CharField(max_length=100, blank=True, default="")
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    remark = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_history_logs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.order.order_no} - {self.action} - {self.field_name}"