from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class Expense(models.Model):
    TYPE_OTHER = "OTHER"
    TYPE_BATCH = "BATCH"
    TYPE_OPERATING = "OPERATING"

    TYPE_CHOICES = [
        (TYPE_OTHER, "Other Expense"),
        (TYPE_BATCH, "Stock In Expense"),
        (TYPE_OPERATING, "Operating Expense"),
    ]

    # Other Expense: simple daily costs entered by staff.
    OTHER_GRAB = "GRAB"
    OTHER_UTILITY = "UTILITY"
    OTHER_FOOD = "FOOD"
    OTHER_STATIONERY = "STATIONERY"
    OTHER_REPAIR = "REPAIR"
    OTHER_TRANSPORT = "TRANSPORT"
    OTHER_SMALL_PURCHASE = "SMALL_PURCHASE"
    OTHER_OTHER = "OTHER"

    OTHER_CATEGORY_CHOICES = [
        (OTHER_GRAB, "Grab Delivery"),
        (OTHER_UTILITY, "Utility"),
        (OTHER_FOOD, "Food"),
        (OTHER_STATIONERY, "Stationery"),
        (OTHER_REPAIR, "Repair"),
        (OTHER_TRANSPORT, "Transport"),
        (OTHER_SMALL_PURCHASE, "Small Purchase"),
        (OTHER_OTHER, "Other"),
    ]

    # Operating Expense: recurring or production-related business costs.
    OPERATING_SALARY = "SALARY"
    OPERATING_COMMISSION = "COMMISSION"
    OPERATING_CLOTH_CUTTING = "CLOTH_CUTTING"
    OPERATING_RENT = "RENT"
    OPERATING_ELECTRICITY = "ELECTRICITY"
    OPERATING_INTERNET = "INTERNET"
    OPERATING_MARKETING = "MARKETING"
    OPERATING_EQUIPMENT = "EQUIPMENT"
    OPERATING_OTHER = "OTHER"

    OPERATING_CATEGORY_CHOICES = [
        (OPERATING_SALARY, "Staff Salary"),
        (OPERATING_COMMISSION, "Staff Commission"),
        (OPERATING_CLOTH_CUTTING, "Cloth Cutting Expense"),
        (OPERATING_RENT, "Rent"),
        (OPERATING_ELECTRICITY, "Electricity"),
        (OPERATING_INTERNET, "Internet"),
        (OPERATING_MARKETING, "Marketing"),
        (OPERATING_EQUIPMENT, "Equipment"),
        (OPERATING_OTHER, "Other"),
    ]

    created_at = models.DateTimeField(default=timezone.now)

    # Business date selected by the user. This can differ from the exact
    # date/time when the record was entered into the system.
    expense_date = models.DateField(default=timezone.localdate)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="finance_expenses_created",
    )

    expense_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    note = models.TextField(blank=True, default="")

    category = models.CharField(
        max_length=30,
        choices=OTHER_CATEGORY_CHOICES + OPERATING_CATEGORY_CHOICES,
        blank=True,
        default="",
    )

    batch = models.ForeignKey(
        "inventory.InventoryBatch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expense_rows",
    )

    batch_created_at = models.DateTimeField(null=True, blank=True)
    batch_total_cloth = models.PositiveIntegerField(default=0)
    batch_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    batch_delivery_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    batch_other_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    STATUS_PENDING = "PENDING"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Cost"),
        (STATUS_COMPLETED, "Completed"),
    ]

    SOURCE_INVENTORY = "INVENTORY"
    SOURCE_FABRIC = "FABRIC"
    SOURCE_CHOICES = [
        (SOURCE_INVENTORY, "Inventory Batch"),
        (SOURCE_FABRIC, "Fabric Stock In"),
    ]

    expense_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    stock_source_type = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        blank=True,
        default="",
    )
    source_reference = models.CharField(max_length=120, blank=True, default="")
    supplier_name = models.CharField(max_length=160, blank=True, default="")
    received_date = models.DateField(null=True, blank=True)
    fabric_receipt_ids = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        permissions = [
            ("view_finance_nav", "Can view finance navbar"),
            ("view_expense_summary_nav", "Can view expense summary navbar"),
            ("view_other_expense_nav", "Can view other expense navbar"),
            ("view_batch_expense_nav", "Can view batch expense navbar"),
            ("view_operating_expense_nav", "Can view operating expense navbar"),
            ("view_profit_dashboard_nav", "Can view profit dashboard navbar"),
        ]

    def __str__(self):
        return f"{self.get_expense_type_display()} - ${self.amount}"