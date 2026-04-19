from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import redirect, render

from .forms import BatchExpenseForm, ExpenseFilterForm, OperatingExpenseForm, OtherExpenseForm
from .models import Expense


def _can_create_other(user):
    return user.is_staff or user.is_superuser or user.has_perm("finance.can_create_other_expense")


def _can_create_batch(user):
    return user.is_superuser or user.has_perm("finance.can_create_batch_expense")


def _can_create_operating(user):
    return user.is_superuser or user.has_perm("finance.can_create_operating_expense")


def _get_revenue_total():
    try:
        from orders.models import Order
    except Exception:
        return Decimal("0.00")

    field_candidates = ["total_selling_price", "total_price", "price", "grand_total"]
    for field_name in field_candidates:
        try:
            value = Order.objects.aggregate(total=Sum(field_name))["total"]
            if value is not None:
                return value
        except Exception:
            continue
    return Decimal("0.00")


def _get_total_cloth_sold():
    try:
        from orders.models import OrderItem
        value = OrderItem.objects.aggregate(total=Sum("quantity"))["total"]
        return value or 0
    except Exception:
        pass

    try:
        from orders.models import Order
        value = Order.objects.aggregate(total=Sum("quantity"))["total"]
        return value or 0
    except Exception:
        return 0


def _get_total_inventory():
    try:
        from inventory.models import Inventory
        value = Inventory.objects.aggregate(total=Sum("quantity"))["total"]
        return value or 0
    except Exception:
        pass

    try:
        from inventory.models import InventoryItem
        value = InventoryItem.objects.aggregate(total=Sum("quantity"))["total"]
        return value or 0
    except Exception:
        return 0


def _get_expense_chart_data():
    from django.utils import timezone

    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=29)

    qs = (
        Expense.objects.filter(created_at__date__gte=start_date, created_at__date__lte=end_date)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Sum("amount"))
        .order_by("day")
    )

    expense_map = {row["day"]: float(row["total"] or 0) for row in qs}

    labels = []
    values = []
    current = start_date
    while current <= end_date:
        labels.append(current.strftime("%d %b"))
        values.append(expense_map.get(current, 0))
        current += timedelta(days=1)

    return labels, values


def _get_revenue_chart_data():
    from django.utils import timezone

    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=29)

    labels = []
    values = []

    try:
        from orders.models import Order

        money_field = None
        for field_name in ["total_selling_price", "total_price", "price", "grand_total"]:
            try:
                Order.objects.values(field_name)[:1]
                money_field = field_name
                break
            except Exception:
                continue

        if money_field:
            qs = (
                Order.objects.filter(created_at__date__gte=start_date, created_at__date__lte=end_date)
                .annotate(day=TruncDate("created_at"))
                .values("day")
                .annotate(total=Sum(money_field))
                .order_by("day")
            )
            revenue_map = {row["day"]: float(row["total"] or 0) for row in qs}
        else:
            revenue_map = {}
    except Exception:
        revenue_map = {}

    current = start_date
    while current <= end_date:
        labels.append(current.strftime("%d %b"))
        values.append(revenue_map.get(current, 0))
        current += timedelta(days=1)

    return labels, values


@login_required
def expense_list(request):
    qs = Expense.objects.select_related("created_by", "batch").all()

    form = ExpenseFilterForm(request.GET or None)
    if form.is_valid():
        date_from = form.cleaned_data.get("date_from")
        date_to = form.cleaned_data.get("date_to")
        expense_type = form.cleaned_data.get("expense_type")
        created_by = (form.cleaned_data.get("created_by") or "").strip()
        keyword = (form.cleaned_data.get("keyword") or "").strip()

        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        if expense_type:
            qs = qs.filter(expense_type=expense_type)
        if created_by:
            qs = qs.filter(
                Q(created_by__username__icontains=created_by)
                | Q(created_by__first_name__icontains=created_by)
                | Q(created_by__last_name__icontains=created_by)
            )
        if keyword:
            qs = qs.filter(
                Q(note__icontains=keyword)
                | Q(category__icontains=keyword)
                | Q(batch__batch_code__icontains=keyword)
            )

    total_expense = qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    context = {
        "form": form,
        "expenses": qs[:300],
        "total_expense": total_expense,
        "can_create_other": _can_create_other(request.user),
        "can_create_batch": _can_create_batch(request.user),
        "can_create_operating": _can_create_operating(request.user),
    }
    return render(request, "finance/expense_list.html", context)


@login_required
def create_other_expense(request):
    if not _can_create_other(request.user):
        messages.error(request, "You do not have permission to create other expense.")
        return redirect("expense_list")

    if request.method == "POST":
        form = OtherExpenseForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.expense_type = Expense.TYPE_OTHER
            obj.created_by = request.user
            obj.save()
            messages.success(request, "Other expense created successfully.")
            return redirect("expense_list")
    else:
        form = OtherExpenseForm()

    return render(request, "finance/expense_form.html", {
        "title": "Create Other Expense",
        "form": form,
    })


@login_required
def create_operating_expense(request):
    if not _can_create_operating(request.user):
        messages.error(request, "You do not have permission to create operating expense.")
        return redirect("expense_list")

    if request.method == "POST":
        form = OperatingExpenseForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.expense_type = Expense.TYPE_OPERATING
            obj.created_by = request.user
            obj.save()
            messages.success(request, "Operating expense created successfully.")
            return redirect("expense_list")
    else:
        form = OperatingExpenseForm()

    return render(request, "finance/expense_form.html", {
        "title": "Create Operating Expense",
        "form": form,
    })


@login_required
def create_batch_expense(request):
    if not _can_create_batch(request.user):
        messages.error(request, "You do not have permission to create batch expense.")
        return redirect("expense_list")

    if request.method == "POST":
        form = BatchExpenseForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.expense_type = Expense.TYPE_BATCH
            obj.created_by = request.user
            obj.save()
            messages.success(request, "Batch expense created successfully.")
            return redirect("expense_list")
    else:
        form = BatchExpenseForm()

    return render(request, "finance/expense_form.html", {
        "title": "Create Batch Expense",
        "form": form,
    })


@login_required
def profit_dashboard(request):
    revenue_total = _get_revenue_total()
    expense_total = Expense.objects.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    profit_total = revenue_total - expense_total

    cloth_sold = _get_total_cloth_sold()
    total_inventory = _get_total_inventory()

    expense_by_type = {
        "other": Expense.objects.filter(expense_type=Expense.TYPE_OTHER).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
        "batch": Expense.objects.filter(expense_type=Expense.TYPE_BATCH).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
        "operating": Expense.objects.filter(expense_type=Expense.TYPE_OPERATING).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
    }

    recent_expenses = Expense.objects.select_related("created_by", "batch").all()[:8]
    revenue_labels, revenue_values = _get_revenue_chart_data()
    expense_labels, expense_values = _get_expense_chart_data()

    return render(request, "finance/profit_dashboard.html", {
        "revenue_total": revenue_total,
        "expense_total": expense_total,
        "profit_total": profit_total,
        "cloth_sold": cloth_sold,
        "total_inventory": total_inventory,
        "expense_by_type": expense_by_type,
        "recent_expenses": recent_expenses,
        "revenue_labels": revenue_labels,
        "revenue_values": revenue_values,
        "expense_labels": expense_labels,
        "expense_values": expense_values,
    })