from decimal import Decimal

from django.contrib.auth.models import User
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


ZERO = Decimal("0.00")


class UserProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    signature = models.ImageField(
        upload_to="signatures/",
        blank=True,
        null=True,
    )

    # Staff / payroll information
    is_staff_employee = models.BooleanField(
        default=False,
        help_text="Enable this user as an office staff member for payroll.",
    )
    join_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date this staff member joined the office.",
    )
    left_date = models.DateField(
        null=True,
        blank=True,
        help_text="Optional date the staff member stopped working.",
    )
    staff_note = models.TextField(
        blank=True,
        default="",
    )

    def __str__(self):
        return self.user.get_full_name() or self.user.username

    @property
    def staff_name(self):
        return self.user.get_full_name() or self.user.username

    @property
    def current_salary_record(self):
        today = timezone.localdate()
        return (
            self.salary_history
            .filter(effective_date__lte=today)
            .order_by("-effective_date", "-id")
            .first()
        )

    @property
    def current_salary(self):
        record = self.current_salary_record
        return Decimal(record.salary or 0) if record else ZERO

    def salary_on_date(self, target_date):
        record = (
            self.salary_history
            .filter(effective_date__lte=target_date)
            .order_by("-effective_date", "-id")
            .first()
        )
        return Decimal(record.salary or 0) if record else ZERO


class StaffSalaryHistory(models.Model):
    """
    Every starting salary / salary upgrade is a new row.
    Old salary records are never overwritten.
    """
    staff = models.ForeignKey(
        UserProfile,
        on_delete=models.CASCADE,
        related_name="salary_history",
    )
    salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(ZERO)],
    )
    effective_date = models.DateField()
    note = models.CharField(
        max_length=255,
        blank=True,
        default="",
    )
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_salary_records_created",
    )

    class Meta:
        ordering = ["-effective_date", "-id"]
        indexes = [
            models.Index(fields=["staff", "effective_date"]),
        ]

    def __str__(self):
        return f"{self.staff.staff_name} - ${self.salary} from {self.effective_date}"


class StaffPayroll(models.Model):
    STATUS_OPEN = "OPEN"
    STATUS_FIRST_PAID = "FIRST_PAID"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_CANCELLED = "CANCELLED"

    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_FIRST_PAID, "First Payment Paid"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    MONTH_CHOICES = [
        (1, "January"),
        (2, "February"),
        (3, "March"),
        (4, "April"),
        (5, "May"),
        (6, "June"),
        (7, "July"),
        (8, "August"),
        (9, "September"),
        (10, "October"),
        (11, "November"),
        (12, "December"),
    ]

    staff = models.ForeignKey(
        UserProfile,
        on_delete=models.PROTECT,
        related_name="payrolls",
    )
    year = models.PositiveIntegerField()
    month = models.PositiveIntegerField(choices=MONTH_CHOICES)

    # Salary snapshot for this month. Later salary upgrades do not change it.
    base_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )

    # First payment is entered manually.
    first_payment = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    first_payment_date = models.DateField(null=True, blank=True)

    # Final payment inputs are manual.
    commission = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    bonus = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    deduction = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )

    # Actual final payment amount paid.
    final_payment = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=ZERO,
        validators=[MinValueValidator(ZERO)],
    )
    final_payment_date = models.DateField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_OPEN,
    )
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_payrolls_created",
    )

    class Meta:
        ordering = ["-year", "-month", "staff__user__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["staff", "year", "month"],
                name="unique_staff_monthly_payroll",
            ),
        ]
        indexes = [
            models.Index(fields=["year", "month", "status"]),
        ]

    def __str__(self):
        return f"{self.staff.staff_name} - {self.get_month_display()} {self.year}"

    @property
    def remaining_salary(self):
        # Exact requested rule:
        # Remaining = Base Salary - Manual First Payment
        remaining = Decimal(self.base_salary or 0) - Decimal(self.first_payment or 0)
        return max(remaining, ZERO)

    @property
    def final_due(self):
        total = (
            Decimal(self.remaining_salary or 0)
            + Decimal(self.commission or 0)
            + Decimal(self.bonus or 0)
            - Decimal(self.deduction or 0)
        )
        return max(total, ZERO)

    @property
    def total_payroll_amount(self):
        return max(
            Decimal(self.base_salary or 0)
            + Decimal(self.commission or 0)
            + Decimal(self.bonus or 0)
            - Decimal(self.deduction or 0),
            ZERO,
        )


class StaffPayrollPayment(models.Model):
    TYPE_FIRST = "FIRST"
    TYPE_FINAL = "FINAL"

    TYPE_CHOICES = [
        (TYPE_FIRST, "First Payment"),
        (TYPE_FINAL, "Final Payment"),
    ]

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

    payroll = models.ForeignKey(
        StaffPayroll,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    payment_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    payment_date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    payment_method = models.CharField(
        max_length=20,
        choices=METHOD_CHOICES,
        default=METHOD_CASH,
    )
    reference = models.CharField(max_length=120, blank=True, default="")
    note = models.TextField(blank=True, default="")
    finance_expense_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        editable=False,
    )
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_payroll_payments_created",
    )

    class Meta:
        ordering = ["-payment_date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["payroll", "payment_type"],
                name="unique_staff_payroll_payment_type",
            ),
        ]

    def __str__(self):
        return (
            f"{self.payroll.staff.staff_name} - "
            f"{self.get_payment_type_display()} - ${self.amount}"
        )
