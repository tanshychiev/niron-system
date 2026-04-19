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

    # operating
    category = models.CharField(
        max_length=30,
        choices=OPERATING_CATEGORY_CHOICES,
        blank=True,
        default="",
    )

    # batch
    batch = models.ForeignKey(
        "inventory.InventoryBatch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expense_rows",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        permissions = [
            ("can_create_other_expense", "Can create other expense"),
            ("can_create_batch_expense", "Can create batch expense"),
            ("can_create_operating_expense", "Can create operating expense"),
        ]

    def __str__(self):
        return f"{self.get_expense_type_display()} - ${self.amount}"

    @property
    def display_title(self):
        if self.expense_type == self.TYPE_BATCH:
            if self.batch:
                return f"Batch Expense - {self.batch}"
            return "Batch Expense"
        if self.expense_type == self.TYPE_OPERATING:
            return self.get_category_display() or "Operating Expense"
        return "Other Expense"

    @property
    def display_type_badge(self):
        return self.get_expense_type_display()