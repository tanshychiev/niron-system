from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from customers.models import Customer

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
    SERVICE_RETAIL = "RETAIL"

    SERVICE_CHOICES = [
        (SERVICE_FULL, "Full Order"),
        (SERVICE_FILM_ONLY, "Film Only"),
        (SERVICE_PRINT_HEATPRESS, "Print & Heat Press"),
        (SERVICE_RETAIL, "Retail Sale"),
    ]

    PAYMENT_PENDING = "PENDING"
    PAYMENT_PARTIAL = "PARTIAL"
    PAYMENT_PAID = "PAID"

    PAYMENT_CHOICES = [
        (PAYMENT_PENDING, "Pending"),
        (PAYMENT_PARTIAL, "Partial"),
        (PAYMENT_PAID, "Paid"),
    ]

    # ===== BASIC =====
    order_no = models.CharField(max_length=50, unique=True, blank=True)
    order_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_NIRON)
    service_type = models.CharField(max_length=30, choices=SERVICE_CHOICES, default=SERVICE_FULL)

    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )

    customer_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30, blank=True, default="")
    customer_location = models.CharField(max_length=255, blank=True, default="")
    deadline = models.DateField()

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

    payment_status = models.CharField(
        max_length=20,
        choices=PAYMENT_CHOICES,
        default=PAYMENT_PENDING,
    )

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
        return (
            (self.total_amount or Decimal("0"))
            - (self.deposit_amount or Decimal("0"))
            - (self.paid_amount or Decimal("0"))
        )

    @property
    def payment_status_display(self):
        if self.balance_amount <= 0:
            return "Paid"
        if (self.deposit_amount or Decimal("0")) > 0 or (self.paid_amount or Decimal("0")) > 0:
            return "Partial"
        return "Pending"

    @property
    def remaining_pcs(self):
        return (self.total_pcs or Decimal("0")) - (self.done_pcs or Decimal("0"))

    def save(self, *args, **kwargs):
        if not self.order_no:
            last_id = (
                Order.objects.order_by("-id").values_list("id", flat=True).first() or 0
            ) + 1
            self.order_no = f"NR-{last_id:06d}"

        if self.balance_amount <= 0:
            self.payment_status = self.PAYMENT_PAID
        elif (self.deposit_amount or Decimal("0")) > 0 or (self.paid_amount or Decimal("0")) > 0:
            self.payment_status = self.PAYMENT_PARTIAL
        else:
            self.payment_status = self.PAYMENT_PENDING

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
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
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
    color = models.ForeignKey(Color, on_delete=models.PROTECT, related_name="order_items", blank=True, null=True)
    size = models.ForeignKey(Size, on_delete=models.PROTECT, related_name="order_items", blank=True, null=True)

    film_item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="order_film_items",
        blank=True,
        null=True,
    )
    film_meter = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    material_item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="order_material_items",
        blank=True,
        null=True,
    )

    quantity = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    done_qty = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        base_name = (
            self.description
            or str(self.shirt_item or "")
            or str(self.material_item or "")
            or str(self.film_item or "")
            or f"Item {self.pk}"
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
            if Decimal(self.quantity or 0) < 1:
                raise ValidationError({"quantity": "Full Order requires quantity."})
            if Decimal(self.unit_price or 0) <= 0:
                raise ValidationError({"unit_price": "Full Order requires unit price."})

            self.quantity = Decimal(self.quantity or 0).quantize(Decimal("1"))
            self.film_item = None
            self.material_item = None
            self.film_meter = Decimal("0.00")

        elif service_type == Order.SERVICE_FILM_ONLY:
            if not self.film_item:
                raise ValidationError({"film_item": "Film Only requires film item."})
            if Decimal(self.film_meter or 0) <= 0:
                raise ValidationError({"film_meter": "Film Only requires film meter."})
            if Decimal(self.unit_price or 0) <= 0:
                raise ValidationError({"unit_price": "Film Only requires unit price."})

            self.quantity = Decimal("0")
            self.done_qty = Decimal("0")
            self.shirt_item = None
            self.material_item = None
            self.color = None
            self.size = None

        elif service_type == Order.SERVICE_PRINT_HEATPRESS:
            if Decimal(self.quantity or 0) < 1:
                raise ValidationError({"quantity": "Print & Heat Press requires quantity."})
            if Decimal(self.unit_price or 0) <= 0:
                raise ValidationError({"unit_price": "Print & Heat Press requires unit price."})

            self.quantity = Decimal(self.quantity or 0).quantize(Decimal("1"))
            self.shirt_item = None
            self.material_item = None
            self.color = None
            self.size = None
            self.film_item = None
            self.film_meter = Decimal("0.00")

        elif service_type == Order.SERVICE_RETAIL:
            has_shirt = bool(self.shirt_item)
            has_material = bool(self.material_item)

            if not has_shirt and not has_material:
                raise ValidationError("Retail Sale requires shirt item OR material item.")
            if has_shirt and has_material:
                raise ValidationError("Retail Sale cannot choose both shirt and material in one row.")
            if Decimal(self.quantity or 0) < 1:
                raise ValidationError({"quantity": "Retail Sale requires quantity."})
            if Decimal(self.unit_price or 0) <= 0:
                raise ValidationError({"unit_price": "Retail Sale requires unit price."})

            self.quantity = Decimal(self.quantity or 0).quantize(Decimal("1"))
            self.film_item = None
            self.film_meter = Decimal("0.00")

            if has_shirt:
                if not self.color:
                    raise ValidationError({"color": "Retail shirt sale requires color."})
                if not self.size:
                    raise ValidationError({"size": "Retail shirt sale requires size."})
                self.material_item = None
            else:
                self.shirt_item = None
                self.color = None
                self.size = None

        if Decimal(self.done_qty or 0) > Decimal(self.quantity or 0):
            raise ValidationError({"done_qty": "Done qty cannot be greater than quantity."})

    def save(self, *args, **kwargs):
        if self.design_id:
            self.order = self.design.order

        def money2(v):
            return Decimal(str(v or "0")).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

        self.unit_price = money2(self.unit_price)

        if self.order and self.order.service_type == Order.SERVICE_FILM_ONLY:
            self.quantity = Decimal("0")
            self.done_qty = Decimal("0")
            self.film_meter = money2(self.film_meter)
            self.line_total = money2(self.film_meter * self.unit_price)
        else:
            self.quantity = Decimal(self.quantity or 0).quantize(Decimal("1"))
            self.film_meter = Decimal("0.00")
            self.line_total = money2(self.quantity * self.unit_price)

        self.full_clean()
        super().save(*args, **kwargs)

    @property
    def remaining_qty(self):
        remaining = Decimal(self.quantity or 0) - Decimal(self.done_qty or 0)
        return remaining if remaining > 0 else Decimal("0")

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