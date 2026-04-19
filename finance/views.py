from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import redirect, render

from .forms import (
    BatchExpenseForm,
    ExpenseFilterForm,
    OperatingExpenseForm,
    OtherExpenseForm,
)
from .models import Expense


def _to_decimal(value):
    if value in (None, ""):
        return Decimal("0.00")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0.00")


def _format_qty(value):
    qty = _to_decimal(value)
    if qty == qty.to_integral_value():
        return str(int(qty))
    return format(qty.normalize(), "f")


def _get_batch_rows(batch):
    return batch.items.select_related("item", "color", "size").filter(is_active=True)


def _get_row_qty_received(row):
    return _to_decimal(row.qty_received)


def _get_row_item_code(row):
    return row.item.code if row.item else ""


def _get_row_item_name(row):
    return row.item.name if row.item else ""


def _get_row_color_name(row):
    return row.color.name if row.color else ""


def _get_row_size_name(row):
    return row.size.name if row.size else ""


def _get_batch_expense_data(batch):
    created_at = batch.received_date
    total_cloth = _to_decimal(batch.total_cloth or 0)

    cost = _to_decimal(batch.total_goods_cost)
    delivery_fee = _to_decimal(batch.shipping_cost)
    other_fee = _to_decimal(batch.extra_cost)
    amount = cost + delivery_fee + other_fee

    return {
        "created_at": created_at,
        "total_cloth": total_cloth,
        "cost": cost,
        "delivery_fee": delivery_fee,
        "other_fee": other_fee,
        "amount": amount,
    }


def _apply_filters(request, qs):
    form = ExpenseFilterForm(request.GET or None)
    if form.is_valid():
        date_from = form.cleaned_data.get("date_from")
        date_to = form.cleaned_data.get("date_to")
        created_by = (form.cleaned_data.get("created_by") or "").strip()
        keyword = (form.cleaned_data.get("keyword") or "").strip()

        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
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
                | Q(batch__batch_no__icontains=keyword)
            )

    return form, qs


def _get_revenue_total():
    try:
        from orders.models import Order
    except Exception:
        return Decimal("0.00")

    for field_name in ["total_selling_price", "total_price", "price", "grand_total"]:
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
        from inventory.models import InventoryBatchItem, InventoryItem

        value = (
            InventoryBatchItem.objects.filter(
                is_active=True,
                batch__is_deleted=False,
                item__item_type=InventoryItem.TYPE_SHIRT,
            ).aggregate(total=Sum("qty_remaining"))["total"]
            or 0
        )
        return value
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
@permission_required("finance.view_expense", raise_exception=True)
def expense_summary(request):
    qs = Expense.objects.select_related("created_by", "batch").all()
    form, qs = _apply_filters(request, qs)
    total_expense = qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    return render(
        request,
        "finance/expense_summary.html",
        {
            "form": form,
            "expenses": qs[:300],
            "total_expense": total_expense,
            "page_title_text": "Expense Summary",
            "page_subtitle_text": "All expense activity in one page",
        },
    )


@login_required
@permission_required("finance.view_expense", raise_exception=True)
def other_expense_list(request):
    qs = Expense.objects.select_related("created_by", "batch").filter(expense_type=Expense.TYPE_OTHER)
    form, qs = _apply_filters(request, qs)
    total_expense = qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    return render(
        request,
        "finance/expense_type_list.html",
        {
            "form": form,
            "expenses": qs[:300],
            "total_expense": total_expense,
            "page_title_text": "Other Expense",
            "page_subtitle_text": "Other expense records",
            "create_url_name": "create_other_expense",
            "create_label": "+ Create Other Expense",
            "can_create": request.user.has_perm("finance.add_other_expense"),
        },
    )


@login_required
@permission_required("finance.view_expense", raise_exception=True)
def batch_expense_list(request):
    qs = Expense.objects.select_related("created_by", "batch").filter(expense_type=Expense.TYPE_BATCH)
    form, qs = _apply_filters(request, qs)
    total_expense = qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    return render(
        request,
        "finance/expense_type_list.html",
        {
            "form": form,
            "expenses": qs[:300],
            "total_expense": total_expense,
            "page_title_text": "Batch Expense",
            "page_subtitle_text": "Batch expense records linked to inventory batch",
            "create_url_name": "create_batch_expense",
            "create_label": "+ Create Batch Expense",
            "can_create": request.user.has_perm("finance.add_batch_expense"),
        },
    )


@login_required
@permission_required("finance.view_expense", raise_exception=True)
def operating_expense_list(request):
    qs = Expense.objects.select_related("created_by", "batch").filter(expense_type=Expense.TYPE_OPERATING)
    form, qs = _apply_filters(request, qs)
    total_expense = qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    return render(
        request,
        "finance/expense_type_list.html",
        {
            "form": form,
            "expenses": qs[:300],
            "total_expense": total_expense,
            "page_title_text": "Operating Expense",
            "page_subtitle_text": "Salary, commission, boosting, rent and other operating expense",
            "create_url_name": "create_operating_expense",
            "create_label": "+ Create Operating Expense",
            "can_create": request.user.has_perm("finance.add_operating_expense"),
        },
    )


@login_required
@permission_required("finance.add_other_expense", raise_exception=True)
def create_other_expense(request):
    if request.method == "POST":
        form = OtherExpenseForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.expense_type = Expense.TYPE_OTHER
            obj.created_by = request.user
            obj.save()
            messages.success(request, "Other expense created successfully.")
            return redirect("other_expense_list")
    else:
        form = OtherExpenseForm()

    return render(
        request,
        "finance/expense_form.html",
        {
            "title": "Create Other Expense",
            "form": form,
            "back_url": "other_expense_list",
        },
    )


@login_required
@permission_required("finance.add_batch_expense", raise_exception=True)
def create_batch_expense(request):
    if request.method == "POST":
        form = BatchExpenseForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.expense_type = Expense.TYPE_BATCH
            obj.created_by = request.user

            batch = obj.batch
            if batch:
                detail = _get_batch_expense_data(batch)

                manual_cost = request.POST.get("batch_cost_manual")
                manual_delivery_fee = request.POST.get("batch_delivery_fee_manual")
                manual_other_fee = request.POST.get("batch_other_fee_manual")

                cost = _to_decimal(manual_cost) if manual_cost not in (None, "") else detail["cost"]
                delivery_fee = _to_decimal(manual_delivery_fee) if manual_delivery_fee not in (None, "") else detail["delivery_fee"]
                other_fee = _to_decimal(manual_other_fee) if manual_other_fee not in (None, "") else detail["other_fee"]

                obj.batch_created_at = batch.created_at
                obj.batch_total_cloth = detail["total_cloth"]
                obj.batch_cost = cost
                obj.batch_delivery_fee = delivery_fee
                obj.batch_other_fee = other_fee
                obj.amount = cost + delivery_fee + other_fee
            else:
                obj.batch_created_at = None
                obj.batch_total_cloth = Decimal("0")
                obj.batch_cost = Decimal("0.00")
                obj.batch_delivery_fee = Decimal("0.00")
                obj.batch_other_fee = Decimal("0.00")
                obj.amount = Decimal("0.00")

            obj.save()
            messages.success(request, "Batch expense created successfully.")
            return redirect("batch_expense_list")
    else:
        form = BatchExpenseForm()

    return render(
        request,
        "finance/create_batch_expense.html",
        {
            "title": "Create Batch Expense",
            "form": form,
            "back_url": "batch_expense_list",
        },
    )


@login_required
@permission_required("finance.add_operating_expense", raise_exception=True)
def create_operating_expense(request):
    if request.method == "POST":
        form = OperatingExpenseForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.expense_type = Expense.TYPE_OPERATING
            obj.created_by = request.user
            obj.save()
            messages.success(request, "Operating expense created successfully.")
            return redirect("operating_expense_list")
    else:
        form = OperatingExpenseForm()

    return render(
        request,
        "finance/expense_form.html",
        {
            "title": "Create Operating Expense",
            "form": form,
            "back_url": "operating_expense_list",
        },
    )


@login_required
@permission_required("finance.view_expense", raise_exception=True)
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

    return render(
        request,
        "finance/profit_dashboard.html",
        {
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
        },
    )


@login_required
@permission_required("finance.add_batch_expense", raise_exception=True)
def batch_expense_preview(request):
    batch_id = request.GET.get("batch_id")
    if not batch_id:
        return JsonResponse({"error": "Missing batch_id"}, status=400)

    from inventory.models import InventoryBatch, InventoryItem

    try:
        batch = InventoryBatch.objects.prefetch_related(
            "items__item",
            "items__color",
            "items__size",
        ).get(pk=batch_id, is_deleted=False)
    except InventoryBatch.DoesNotExist:
        return JsonResponse({"error": "Batch not found"}, status=404)

    data = _get_batch_expense_data(batch)
    rows = _get_batch_rows(batch)

    rows_data = []
    color_map = {}

    for row in rows:
        if not row.item or row.item.item_type != InventoryItem.TYPE_SHIRT:
            continue

        qty_received = _get_row_qty_received(row)
        color_name = _get_row_color_name(row) or "-"

        rows_data.append(
            {
                "item_code": _get_row_item_code(row) or "-",
                "item_name": _get_row_item_name(row) or "-",
                "color": color_name,
                "size": _get_row_size_name(row) or "-",
                "qty_received": _format_qty(qty_received),
            }
        )

        color_map[color_name] = color_map.get(color_name, Decimal("0")) + qty_received

    color_summary = [
        {"color": color, "qty": _format_qty(qty)}
        for color, qty in color_map.items()
    ]

    return JsonResponse(
        {
            "batch_name": batch.batch_no,
            "created_at": data["created_at"].strftime("%d/%m/%Y") if data["created_at"] else "",
            "total_cloth": _format_qty(data["total_cloth"]),
            "cost": f"{data['cost']:.2f}",
            "delivery_fee": f"{data['delivery_fee']:.2f}",
            "other_fee": f"{data['other_fee']:.2f}",
            "amount": f"{data['amount']:.2f}",
            "rows": rows_data,
            "color_summary": color_summary,
            "color_count": len(color_summary),
        }
    )