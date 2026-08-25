from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Sum
from django.utils import timezone


ZERO = Decimal("0")


def _next_number(model, field_name, prefix, date_value=None):
    date_value = date_value or timezone.localdate()
    base = f"{prefix}-{date_value:%Y%m%d}"
    last = (
        model.objects.filter(**{f"{field_name}__startswith": base})
        .order_by(f"-{field_name}")
        .values_list(field_name, flat=True)
        .first()
    )
    number = 1
    if last:
        try:
            number = int(str(last).rsplit("-", 1)[-1]) + 1
        except (TypeError, ValueError):
            number = model.objects.filter(**{f"{field_name}__startswith": base}).count() + 1
    candidate = f"{base}-{number:03d}"
    while model.objects.filter(**{field_name: candidate}).exists():
        number += 1
        candidate = f"{base}-{number:03d}"
    return candidate


class ProductionSupplier(models.Model):
    name = models.CharField(max_length=150, unique=True)
    phone = models.CharField(max_length=50, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name




class FabricType(models.Model):
    name = models.CharField(max_length=150, unique=True)
    gsm = models.PositiveIntegerField(null=True, blank=True)
    composition = models.CharField(max_length=150, blank=True, default="")
    is_active = models.BooleanField(default=True)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        details = []
        if self.gsm:
            details.append(f"{self.gsm} GSM")
        if self.composition:
            details.append(self.composition)
        return f"{self.name} ({' / '.join(details)})" if details else self.name

class FabricReceipt(models.Model):
    # Manual Fabric purchase / receiving workflow.
    # Default RECEIVED keeps every existing FabricReceipt behaving exactly as before.
    STATUS_WAITING = "WAITING"
    STATUS_RECEIVED = "RECEIVED"
    STATUS_CHOICES = [
        (STATUS_WAITING, "Waiting to Receive"),
        (STATUS_RECEIVED, "Received"),
    ]

    receipt_no = models.CharField(max_length=50, unique=True, blank=True)
    supplier = models.CharField(max_length=150)
    supplier_ref = models.ForeignKey(
        "ProductionSupplier",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fabric_receipts",
    )
    fabric_type = models.ForeignKey(
        "FabricType",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fabric_receipts",
    )
    received_date = models.DateField(default=timezone.localdate)
    # For a saved purchase this is the planned date until staff confirms arrival.
    expected_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_RECEIVED)
    purchase_group = models.CharField(max_length=60, blank=True, default="", db_index=True)
    # Roll weights entered while ordering are stored here until physical receipt.
    pending_roll_weights = models.JSONField(default=list, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_fabric_receipts_received",
    )
    # Legacy snapshot kept so all old receipts and reports remain compatible.
    fabric_name = models.CharField(max_length=150)
    color = models.ForeignKey(
        "inventory.Color",
        on_delete=models.PROTECT,
        related_name="production_fabric_receipts",
    )
    roll_count = models.PositiveIntegerField(default=1)
    total_goods_cost = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    shipping_cost = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    extra_cost = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_fabric_receipts_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_fabric_receipts_updated",
    )

    class Meta:
        ordering = ["-received_date", "-id"]
        permissions = [
            ("view_production_nav", "Can view production navbar"),
            ("view_production_cost", "Can view production costs"),
            ("manage_production_payments", "Can manage production payments"),
        ]

    def __str__(self):
        return self.receipt_no or f"Fabric receipt {self.pk or ''}"

    def save(self, *args, **kwargs):
        if self.supplier_ref_id:
            self.supplier = self.supplier_ref.name
        if self.fabric_type_id:
            self.fabric_name = self.fabric_type.name
        if not self.receipt_no:
            self.receipt_no = _next_number(
                FabricReceipt,
                "receipt_no",
                "FAB",
                self.received_date,
            )
        super().save(*args, **kwargs)

    @property
    def is_waiting(self):
        return self.status == self.STATUS_WAITING

    @property
    def is_received(self):
        return self.status == self.STATUS_RECEIVED

    @property
    def total_cost(self):
        return (
            Decimal(self.total_goods_cost or 0)
            + Decimal(self.shipping_cost or 0)
            + Decimal(self.extra_cost or 0)
        )

    @property
    def cost_per_roll(self):
        count = Decimal(self.roll_count or 0)
        return self.total_cost / count if count > 0 else ZERO

    @property
    def total_weight_kg(self):
        # New receipts store each physical roll weight in FabricRoll.original_qty.
        # Old receipts (created before KG tracking) safely remain 1.000 per roll.
        return (
            self.rolls.aggregate(total=Sum("original_qty"))["total"]
            or ZERO
        )

    @property
    def cost_per_kg(self):
        total_kg = Decimal(self.total_weight_kg or 0)
        return self.total_cost / total_kg if total_kg > 0 else ZERO


class FabricRoll(models.Model):
    STATUS_FULL = "FULL"
    STATUS_PARTIAL = "PARTIAL"
    STATUS_USED = "USED"

    STATUS_CHOICES = [
        (STATUS_FULL, "Full"),
        (STATUS_PARTIAL, "Partial"),
        (STATUS_USED, "Used"),
    ]

    receipt = models.ForeignKey(
        FabricReceipt,
        on_delete=models.PROTECT,
        related_name="rolls",
    )
    roll_code = models.CharField(max_length=70, unique=True)
    original_qty = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=Decimal("1.000"),
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    remaining_qty = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=Decimal("1.000"),
        validators=[MinValueValidator(ZERO)],
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_FULL)
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["receipt__fabric_name", "receipt__color__name", "roll_code"]
        indexes = [
            models.Index(fields=["status", "remaining_qty"]),
            models.Index(fields=["roll_code"]),
        ]

    def __str__(self):
        return f"{self.roll_code} - {self.receipt.fabric_name} / {self.receipt.color.name}"

    def clean(self):
        if self.remaining_qty is not None and self.original_qty is not None:
            if self.remaining_qty < 0:
                raise ValidationError("Remaining quantity cannot be negative.")
            if self.remaining_qty > self.original_qty:
                raise ValidationError("Remaining quantity cannot exceed original roll quantity.")

    def save(self, *args, **kwargs):
        self.full_clean()
        remaining = Decimal(self.remaining_qty or 0)
        original = Decimal(self.original_qty or 0)
        if remaining <= 0:
            self.status = self.STATUS_USED
        elif remaining < original:
            self.status = self.STATUS_PARTIAL
        else:
            self.status = self.STATUS_FULL
        super().save(*args, **kwargs)

    @property
    def reserved_qty(self):
        return (
            self.cutting_usages.filter(applied=False)
            .aggregate(total=Sum("issued_qty"))["total"]
            or ZERO
        )

    @property
    def available_qty(self):
        value = Decimal(self.remaining_qty or 0) - Decimal(self.reserved_qty or 0)
        return max(value, ZERO)

    @property
    def reservation_status(self):
        if self.available_qty <= 0 and self.reserved_qty > 0:
            return "RESERVED"
        if self.reserved_qty > 0:
            return "PARTIAL_RESERVED"
        return "AVAILABLE"

    @property
    def fabric_name(self):
        return self.receipt.fabric_name

    @property
    def color(self):
        return self.receipt.color

    @property
    def unit_cost(self):
        # Fabric quantity is now tracked in KG, so this is cost per KG.
        return self.receipt.cost_per_kg

    @property
    def remaining_value(self):
        return Decimal(self.remaining_qty or 0) * self.unit_cost


class SewingPartner(models.Model):
    name = models.CharField(max_length=150, unique=True)
    phone = models.CharField(max_length=50, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ProductionProject(models.Model):
    STATUS_DRAFT = "DRAFT"
    STATUS_CUTTING = "CUTTING"
    STATUS_CUT_COMPLETE = "CUT_COMPLETE"
    STATUS_SENT = "SENT"
    STATUS_PARTIAL_RETURN = "PARTIAL_RETURN"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_CUTTING, "Cutting"),
        (STATUS_CUT_COMPLETE, "Cut Completed"),
        (STATUS_SENT, "Sent to Sewing"),
        (STATUS_PARTIAL_RETURN, "Partially Returned"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    project_no = models.CharField(max_length=50, unique=True, blank=True)
    finished_item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="production_projects",
        limit_choices_to={"item_type": "SHIRT"},
    )
    fabric_type = models.ForeignKey(
        "FabricType",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="production_projects",
    )
    color = models.ForeignKey(
        "inventory.Color",
        on_delete=models.PROTECT,
        related_name="production_projects",
        null=True,
        blank=True,
        help_text="Legacy first colour; new projects use project_colors.",
    )
    expected_qty = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_projects_created",
    )

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        colors = ", ".join(self.project_colors.values_list("color__name", flat=True)[:3])
        return f"{self.project_no} - {self.finished_item.name} / {colors or 'No colour'}"

    def save(self, *args, **kwargs):
        if not self.project_no:
            self.project_no = _next_number(ProductionProject, "project_no", "PRD")
        super().save(*args, **kwargs)

    @property
    def plan_total(self):
        return self.plan_sizes.aggregate(total=Sum("planned_qty"))["total"] or 0

    @property
    def fabric_issued_qty(self):
        return self.roll_usages.aggregate(total=Sum("issued_qty"))["total"] or ZERO

    @property
    def fabric_returned_qty(self):
        return self.roll_usages.aggregate(total=Sum("returned_qty"))["total"] or ZERO

    @property
    def fabric_consumed_qty(self):
        return self.fabric_issued_qty - self.fabric_returned_qty

    @property
    def fabric_cost(self):
        total = ZERO
        for usage in self.roll_usages.select_related("roll__receipt").all():
            total += usage.consumed_qty * usage.roll.unit_cost
        return total

    @property
    def cut_total(self):
        return self.cut_sizes.aggregate(total=Sum("cut_qty"))["total"] or 0

    @property
    def sent_total(self):
        return self.sewing_jobs.aggregate(total=Sum("lines__sent_qty"))["total"] or 0

    @property
    def returned_good_total(self):
        return (
            self.sewing_jobs.filter(returns__status=SewingReturn.STATUS_STOCKED)
            .aggregate(total=Sum("returns__lines__good_qty"))["total"]
            or 0
        )

    @property
    def damaged_total(self):
        return (
            self.sewing_jobs.exclude(returns__status=SewingReturn.STATUS_CANCELLED)
            .aggregate(total=Sum("returns__lines__damaged_qty"))["total"]
            or 0
        )

    @property
    def missing_total(self):
        return (
            self.sewing_jobs.exclude(returns__status=SewingReturn.STATUS_CANCELLED)
            .aggregate(total=Sum("returns__lines__missing_qty"))["total"]
            or 0
        )

    @property
    def still_with_sewer(self):
        value = int(self.sent_total or 0) - int(self.returned_good_total or 0) - int(self.damaged_total or 0) - int(self.missing_total or 0)
        return max(value, 0)

    @property
    def sewing_cost(self):
        return self.payables.filter(payable_type=ProductionPayable.TYPE_SEWER).aggregate(total=Sum("amount"))["total"] or ZERO

    @property
    def staff_cost(self):
        return self.payables.filter(payable_type=ProductionPayable.TYPE_STAFF).aggregate(total=Sum("amount"))["total"] or ZERO

    @property
    def total_production_cost(self):
        return self.fabric_cost + self.sewing_cost + self.staff_cost

    @property
    def cost_per_finished_piece(self):
        good = Decimal(self.returned_good_total or 0)
        return self.total_production_cost / good if good > 0 else ZERO


class ProductionProjectColor(models.Model):
    project = models.ForeignKey(ProductionProject, on_delete=models.CASCADE, related_name="project_colors")
    color = models.ForeignKey("inventory.Color", on_delete=models.PROTECT, related_name="production_project_colors")
    sort_order = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["sort_order", "color__name", "id"]
        constraints = [models.UniqueConstraint(fields=["project", "color"], name="uniq_production_project_color")]

    def __str__(self):
        return f"{self.project.project_no} / {self.color.name}"

    @property
    def plan_total(self):
        return self.plan_sizes.aggregate(total=Sum("planned_qty"))["total"] or 0

    @property
    def cut_total(self):
        return self.cut_sizes.aggregate(total=Sum("cut_qty"))["total"] or 0

    @property
    def sent_total(self):
        return self.sewing_jobs.aggregate(total=Sum("lines__sent_qty"))["total"] or 0


class ProductionPlanSize(models.Model):
    project_color = models.ForeignKey(
        ProductionProjectColor, on_delete=models.CASCADE, related_name="plan_sizes", null=True, blank=True
    )
    project = models.ForeignKey(
        ProductionProject,
        on_delete=models.CASCADE,
        related_name="plan_sizes",
    )
    size = models.ForeignKey(
        "inventory.Size",
        on_delete=models.PROTECT,
        related_name="production_plan_lines",
    )
    planned_qty = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["size__sort_order", "size__id"]
        constraints = [
            models.UniqueConstraint(fields=["project_color", "size"], name="uniq_project_color_plan_size"),
        ]

    def __str__(self):
        return f"{self.project.project_no} / {self.size.name}: {self.planned_qty}"


class CuttingRollUsage(models.Model):
    project_color = models.ForeignKey(
        ProductionProjectColor, on_delete=models.CASCADE, related_name="roll_usages", null=True, blank=True
    )
    project = models.ForeignKey(
        ProductionProject,
        on_delete=models.CASCADE,
        related_name="roll_usages",
    )
    roll = models.ForeignKey(
        FabricRoll,
        on_delete=models.PROTECT,
        related_name="cutting_usages",
    )
    issued_qty = models.DecimalField(max_digits=10, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))])
    returned_qty = models.DecimalField(max_digits=10, decimal_places=3, default=ZERO, validators=[MinValueValidator(ZERO)])
    roll_qty_before = models.DecimalField(max_digits=10, decimal_places=3, default=ZERO)
    roll_qty_after = models.DecimalField(max_digits=10, decimal_places=3, default=ZERO)
    applied = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["project_color", "roll"], name="uniq_project_color_fabric_roll"),
        ]

    def __str__(self):
        return f"{self.project.project_no} / {self.roll.roll_code}"

    def clean(self):
        issued = Decimal(self.issued_qty or 0)
        returned = Decimal(self.returned_qty or 0)
        if returned > issued:
            raise ValidationError("Returned roll quantity cannot exceed issued quantity.")
        if self.roll_id and self.project_id and self.project_color_id and self.roll.receipt.color_id != self.project_color.color_id:
            raise ValidationError("Fabric roll color must match the production project color.")
        if self.roll_id and not self.applied:
            reserved_elsewhere = (
                self.roll.cutting_usages.filter(applied=False)
                .exclude(pk=self.pk)
                .aggregate(total=Sum("issued_qty"))["total"]
                or ZERO
            )
            available = Decimal(self.roll.remaining_qty or 0) - Decimal(reserved_elsewhere or 0)
            if issued > available:
                raise ValidationError("Issued quantity exceeds the roll's available quantity after reservations.")

    @property
    def consumed_qty(self):
        return Decimal(self.issued_qty or 0) - Decimal(self.returned_qty or 0)


class CuttingSizeLine(models.Model):
    project_color = models.ForeignKey(
        ProductionProjectColor, on_delete=models.CASCADE, related_name="cut_sizes", null=True, blank=True
    )
    project = models.ForeignKey(
        ProductionProject,
        on_delete=models.CASCADE,
        related_name="cut_sizes",
    )
    size = models.ForeignKey(
        "inventory.Size",
        on_delete=models.PROTECT,
        related_name="production_cut_lines",
    )
    cut_qty = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["size__sort_order", "size__id"]
        constraints = [
            models.UniqueConstraint(fields=["project_color", "size"], name="uniq_project_color_cut_size"),
        ]

    def __str__(self):
        return f"{self.project.project_no} / {self.size.name}: {self.cut_qty}"


class SewingJob(models.Model):
    STATUS_DRAFT = "DRAFT"
    STATUS_SENT = "SENT"
    STATUS_PARTIAL = "PARTIAL"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_SENT, "Sent"),
        (STATUS_PARTIAL, "Partially Returned"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    job_no = models.CharField(max_length=50, unique=True, blank=True)
    project = models.ForeignKey(
        ProductionProject,
        on_delete=models.PROTECT,
        related_name="sewing_jobs",
    )
    project_color = models.ForeignKey(
        ProductionProjectColor,
        on_delete=models.PROTECT,
        related_name="sewing_jobs",
        null=True,
        blank=True,
    )
    WORKER_PARTNER = "PARTNER"
    WORKER_STAFF = "STAFF"
    WORKER_CHOICES = [(WORKER_PARTNER, "Sewing Partner"), (WORKER_STAFF, "Internal Staff")]
    worker_type = models.CharField(max_length=20, choices=WORKER_CHOICES, default=WORKER_PARTNER)
    partner = models.ForeignKey(
        SewingPartner,
        on_delete=models.PROTECT,
        related_name="sewing_jobs",
        null=True,
        blank=True,
    )
    staff_name = models.CharField(max_length=150, blank=True, default="")
    sent_date = models.DateField(default=timezone.localdate)
    expected_return_date = models.DateField(null=True, blank=True)
    price_per_piece = models.DecimalField(max_digits=12, decimal_places=4, default=ZERO)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SENT)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_sewing_jobs_created",
    )

    class Meta:
        ordering = ["-sent_date", "-id"]

    def __str__(self):
        return f"{self.job_no} - {self.payee_name}"

    @property
    def payee_name(self):
        if self.worker_type == self.WORKER_STAFF:
            return self.staff_name or "Staff"
        return self.partner.name if self.partner_id else "Sewing partner"

    def clean(self):
        if self.project_color_id and self.project_color.project_id != self.project_id:
            raise ValidationError("Selected colour does not belong to this production project.")
        if self.worker_type == self.WORKER_PARTNER:
            if not self.partner_id:
                raise ValidationError({"partner": "Choose a sewing partner."})
            self.staff_name = ""
        elif self.worker_type == self.WORKER_STAFF:
            if not (self.staff_name or "").strip():
                raise ValidationError({"staff_name": "Enter the staff name."})
            self.partner = None

    def save(self, *args, **kwargs):
        self.full_clean()
        if not self.job_no:
            self.job_no = _next_number(SewingJob, "job_no", "SEW", self.sent_date)
        super().save(*args, **kwargs)

    @property
    def sent_total(self):
        return self.lines.aggregate(total=Sum("sent_qty"))["total"] or 0

    @property
    def good_returned_total(self):
        return self.returns.filter(status=SewingReturn.STATUS_STOCKED).aggregate(total=Sum("lines__good_qty"))["total"] or 0

    @property
    def damaged_total(self):
        return self.returns.exclude(status=SewingReturn.STATUS_CANCELLED).aggregate(total=Sum("lines__damaged_qty"))["total"] or 0

    @property
    def missing_total(self):
        return self.returns.exclude(status=SewingReturn.STATUS_CANCELLED).aggregate(total=Sum("lines__missing_qty"))["total"] or 0

    @property
    def pending_total(self):
        value = int(self.sent_total or 0) - int(self.good_returned_total or 0) - int(self.damaged_total or 0) - int(self.missing_total or 0)
        return max(value, 0)


class SewingJobLine(models.Model):
    job = models.ForeignKey(SewingJob, on_delete=models.CASCADE, related_name="lines")
    size = models.ForeignKey("inventory.Size", on_delete=models.PROTECT, related_name="production_sewing_lines")
    sent_qty = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["size__sort_order", "size__id"]
        constraints = [
            models.UniqueConstraint(fields=["job", "size"], name="uniq_sewing_job_size"),
        ]

    def __str__(self):
        return f"{self.job.job_no} / {self.size.name}: {self.sent_qty}"


class SewingReturn(models.Model):
    STATUS_DRAFT = "DRAFT"
    STATUS_STOCKED = "STOCKED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_STOCKED, "Confirmed & Stocked In"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    return_no = models.CharField(max_length=50, unique=True, blank=True)
    job = models.ForeignKey(SewingJob, on_delete=models.PROTECT, related_name="returns")
    return_date = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    note = models.TextField(blank=True, default="")
    stock_batch = models.OneToOneField(
        "inventory.InventoryBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_sewing_return",
    )
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_sewing_returns_created",
    )
    stocked_at = models.DateTimeField(null=True, blank=True)
    stocked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_sewing_returns_stocked",
    )

    class Meta:
        ordering = ["-return_date", "-id"]

    def __str__(self):
        return f"{self.return_no} - {self.job.job_no}"

    def save(self, *args, **kwargs):
        if not self.return_no:
            self.return_no = _next_number(SewingReturn, "return_no", "RET", self.return_date)
        super().save(*args, **kwargs)

    @property
    def good_total(self):
        return self.lines.aggregate(total=Sum("good_qty"))["total"] or 0

    @property
    def damaged_total(self):
        return self.lines.aggregate(total=Sum("damaged_qty"))["total"] or 0

    @property
    def missing_total(self):
        return self.lines.aggregate(total=Sum("missing_qty"))["total"] or 0


class SewingReturnLine(models.Model):
    sewing_return = models.ForeignKey(SewingReturn, on_delete=models.CASCADE, related_name="lines")
    size = models.ForeignKey("inventory.Size", on_delete=models.PROTECT, related_name="production_return_lines")
    good_qty = models.PositiveIntegerField(default=0)
    damaged_qty = models.PositiveIntegerField(default=0)
    missing_qty = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["size__sort_order", "size__id"]
        constraints = [
            models.UniqueConstraint(fields=["sewing_return", "size"], name="uniq_sewing_return_size"),
        ]

    @property
    def total_accounted(self):
        return int(self.good_qty or 0) + int(self.damaged_qty or 0) + int(self.missing_qty or 0)


class ProductionPayable(models.Model):
    TYPE_SEWER = "SEWER"
    TYPE_STAFF = "STAFF"

    TYPE_CHOICES = [
        (TYPE_SEWER, "Sewing Partner"),
        (TYPE_STAFF, "Production Staff"),
    ]

    payable_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    project = models.ForeignKey(ProductionProject, on_delete=models.PROTECT, related_name="payables")
    sewing_job = models.ForeignKey(SewingJob, on_delete=models.PROTECT, null=True, blank=True, related_name="payables")
    sewing_return = models.OneToOneField(SewingReturn, on_delete=models.PROTECT, null=True, blank=True, related_name="sewing_payable")
    payee_name = models.CharField(max_length=150)
    work_type = models.CharField(max_length=100, blank=True, default="")
    description = models.CharField(max_length=255, blank=True, default="")
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_payables_created",
    )

    class Meta:
        ordering = ["payee_name", "project__project_no", "id"]
        indexes = [models.Index(fields=["payable_type", "payee_name"])]

    def __str__(self):
        return f"{self.get_payable_type_display()} / {self.payee_name} / {self.project.project_no}"

    @property
    def balance(self):
        value = Decimal(self.amount or 0) - Decimal(self.paid_amount or 0)
        return value if value > 0 else ZERO

    @property
    def payment_status(self):
        if self.balance <= 0 and Decimal(self.amount or 0) > 0:
            return "PAID"
        if Decimal(self.paid_amount or 0) > 0:
            return "PARTIAL"
        return "UNPAID"


class ProductionPaymentBatch(models.Model):
    METHOD_CASH = "CASH"
    METHOD_ABA = "ABA"
    METHOD_BANK = "BANK"
    METHOD_OTHER = "OTHER"

    METHOD_CHOICES = [
        (METHOD_CASH, "Cash"),
        (METHOD_ABA, "ABA"),
        (METHOD_BANK, "Bank Transfer"),
        (METHOD_OTHER, "Other"),
    ]

    payment_no = models.CharField(max_length=50, unique=True, blank=True)
    payable_type = models.CharField(max_length=20, choices=ProductionPayable.TYPE_CHOICES)
    payee_name = models.CharField(max_length=150)
    payment_date = models.DateField(default=timezone.localdate)
    payment_method = models.CharField(max_length=20, choices=METHOD_CHOICES, default=METHOD_CASH)
    reference = models.CharField(max_length=100, blank=True, default="")
    note = models.TextField(blank=True, default="")
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    finance_expense_id = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_payment_batches_created",
    )

    class Meta:
        ordering = ["-payment_date", "-id"]

    def __str__(self):
        return f"{self.payment_no} - {self.payee_name}"

    def save(self, *args, **kwargs):
        if not self.payment_no:
            prefix = "PAY-SEW" if self.payable_type == ProductionPayable.TYPE_SEWER else "PAY-STF"
            self.payment_no = _next_number(
                ProductionPaymentBatch,
                "payment_no",
                prefix,
                self.payment_date,
            )
        super().save(*args, **kwargs)


class ProductionPaymentAllocation(models.Model):
    payment_batch = models.ForeignKey(ProductionPaymentBatch, on_delete=models.CASCADE, related_name="allocations")
    payable = models.ForeignKey(ProductionPayable, on_delete=models.PROTECT, related_name="payment_allocations")
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["payment_batch", "payable"], name="uniq_payment_batch_payable"),
        ]

    def __str__(self):
        return f"{self.payment_batch.payment_no} / {self.payable_id} / {self.amount}"


class ProductionExpense(models.Model):
    CATEGORY_SUPPLIER = "SUPPLIER"
    CATEGORY_SEWING = "SEWING"
    CATEGORY_STAFF_COMMISSION = "STAFF_COMMISSION"
    CATEGORY_OTHER = "OTHER"
    CATEGORY_CHOICES = [
        (CATEGORY_SUPPLIER, "Supplier Purchase"),
        (CATEGORY_SEWING, "Sewing Partner Expense"),
        (CATEGORY_STAFF_COMMISSION, "Staff Commission"),
        (CATEGORY_OTHER, "Other Production Expense"),
    ]

    expense_date = models.DateField(default=timezone.localdate)
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES)
    supplier = models.ForeignKey(
        ProductionSupplier, on_delete=models.PROTECT, null=True, blank=True, related_name="expenses"
    )
    sewing_partner = models.ForeignKey(
        SewingPartner, on_delete=models.PROTECT, null=True, blank=True, related_name="expenses"
    )
    project = models.ForeignKey(
        ProductionProject, on_delete=models.SET_NULL, null=True, blank=True, related_name="expense_records"
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    payment_method = models.CharField(max_length=50, blank=True, default="")
    reference = models.CharField(max_length=120, blank=True, default="")
    note = models.TextField(blank=True, default="")
    finance_expense_id = models.PositiveIntegerField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="production_expenses_created"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-expense_date", "-id"]
        permissions = [("view_production_expense", "Can view production expenses")]

    def clean(self):
        super().clean()
        if self.category == self.CATEGORY_SUPPLIER and not self.supplier_id:
            raise ValidationError({"supplier": "Select a supplier."})
        if self.category == self.CATEGORY_SEWING and not self.sewing_partner_id:
            raise ValidationError({"sewing_partner": "Select a sewing partner."})

    def __str__(self):
        return f"{self.get_category_display()} - {self.amount}"
