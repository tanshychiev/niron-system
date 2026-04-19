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
        (TYPE_BATCH, "Batch Expense"),
        (TYPE_OPERATING, "Operating Expense"),
    ]

    OPERATING_SALARY = "SALARY"
    OPERATING_COMMISSION = "COMMISSION"
    OPERATING_BOOSTING = "BOOSTING"
    OPERATING_RENT = "RENT"
    OPERATING_UTILITY = "UTILITY"
    OPERATING_OTHER = "OTHER"

    OPERATING_CATEGORY_CHOICES = [
        (OPERATING_SALARY, "Staff Salary"),
        (OPERATING_COMMISSION, "Commission"),
        (OPERATING_BOOSTING, "Boosting"),
        (OPERATING_RENT, "Rent"),
        (OPERATING_UTILITY, "Utility"),
        (OPERATING_OTHER, "Other"),
    ]

    created_at = models.DateTimeField(default=timezone.now)
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
        choices=OPERATING_CATEGORY_CHOICES,
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

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.get_expense_type_display()} - ${self.amount}"