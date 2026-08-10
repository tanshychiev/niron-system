from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from openpyxl import Workbook

from .forms import (
    BatchExpenseForm,
    BatchExpenseCostForm,
    ExpenseFilterForm,
    OperatingExpenseForm,
    OtherExpenseForm,
)
from .models import Expense
from production.models import FabricReceipt


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
        expense_type = (form.cleaned_data.get("expense_type") or "").strip()

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

        if expense_type:
            qs = qs.filter(expense_type=expense_type)

    return form, qs


def _get_total_inventory():
    try:
        from inventory.models import InventoryBatchItem, InventoryItem

        return (
            InventoryBatchItem.objects.filter(
                is_active=True,
                batch__is_deleted=False,
                item__item_type=InventoryItem.TYPE_SHIRT,
            ).aggregate(total=Sum("qty_remaining"))["total"]
            or 0
        )
    except Exception:
        return 0


def _get_expense_chart_data():
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=29)

    qs = (
        Expense.objects.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        )
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


@login_required
@permission_required("finance.view_expense_summary_nav", raise_exception=True)
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
@permission_required("finance.view_other_expense_nav", raise_exception=True)
def other_expense_list(request):
    qs = Expense.objects.select_related("created_by", "batch").filter(
        expense_type=Expense.TYPE_OTHER
    )
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
            "can_create": request.user.has_perm("finance.add_expense"),
        },
    )


@login_required
@permission_required("finance.view_batch_expense_nav", raise_exception=True)
def batch_expense_list(request):
    """Stock In Expense list synced from cloth, printing material and fabric Stock In."""
    qs = Expense.objects.select_related("created_by", "batch").filter(
        expense_type=Expense.TYPE_BATCH
    )

    status = (request.GET.get("status") or "").strip().upper()
    stock_type = (request.GET.get("stock_type") or "").strip().upper()
    supplier = (request.GET.get("supplier") or "").strip()
    month = (request.GET.get("month") or "").strip()
    sort = (request.GET.get("sort") or "-received_date").strip()

    if status in {Expense.STATUS_PENDING, Expense.STATUS_COMPLETED}:
        qs = qs.filter(expense_status=status)

    valid_sources = {
        Expense.SOURCE_FABRIC,
        Expense.SOURCE_INVENTORY,
        "CLOTH",
        "PRINTING",
    }
    if stock_type in valid_sources:
        if stock_type in {"CLOTH", "PRINTING"}:
            qs = qs.filter(stock_source_type=Expense.SOURCE_INVENTORY)
            if stock_type == "CLOTH":
                qs = qs.filter(
                    Q(note__icontains="cloth")
                    | Q(source_reference__icontains="cloth")
                    | Q(batch__items__item__item_type="SHIRT")
                )
            else:
                qs = qs.exclude(batch__items__item__item_type="SHIRT")
        else:
            qs = qs.filter(stock_source_type=stock_type)

    if supplier:
        qs = qs.filter(
            Q(supplier_name__icontains=supplier)
            | Q(batch__supplier__icontains=supplier)
            | Q(batch__supplier_ref__name__icontains=supplier)
        )

    if month:
        try:
            year, month_no = [int(x) for x in month.split("-", 1)]
            qs = qs.filter(
                Q(received_date__year=year, received_date__month=month_no)
                | Q(received_date__isnull=True, created_at__year=year, created_at__month=month_no)
            )
        except (TypeError, ValueError):
            pass

    allowed_sort = {
        "date": "received_date",
        "-date": "-received_date",
        "supplier": "supplier_name",
        "-supplier": "-supplier_name",
        "amount": "amount",
        "-amount": "-amount",
        "status": "expense_status",
        "-status": "-expense_status",
    }
    qs = qs.distinct().order_by(allowed_sort.get(sort, "-received_date"), "-id")

    total_expense = qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    pending_count = Expense.objects.filter(
        expense_type=Expense.TYPE_BATCH,
        expense_status=Expense.STATUS_PENDING,
    ).count()
    completed_count = Expense.objects.filter(
        expense_type=Expense.TYPE_BATCH,
        expense_status=Expense.STATUS_COMPLETED,
    ).count()

    return render(
        request,
        "finance/batch_expense_list.html",
        {
            "expenses": qs[:500],
            "total_expense": total_expense,
            "pending_count": pending_count,
            "completed_count": completed_count,
            "selected_status": status,
            "selected_stock_type": stock_type,
            "selected_supplier": supplier,
            "selected_month": month,
            "selected_sort": sort,
        },
    )

@login_required
@permission_required("finance.view_operating_expense_nav", raise_exception=True)
def operating_expense_list(request):
    qs = Expense.objects.select_related("created_by", "batch").filter(
        expense_type=Expense.TYPE_OPERATING
    )
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
            "can_create": request.user.has_perm("finance.add_expense"),
        },
    )


@login_required
@permission_required("finance.add_expense", raise_exception=True)
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
@permission_required("finance.add_expense", raise_exception=True)
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

                cost = (
                    _to_decimal(manual_cost)
                    if manual_cost not in (None, "")
                    else detail["cost"]
                )
                delivery_fee = (
                    _to_decimal(manual_delivery_fee)
                    if manual_delivery_fee not in (None, "")
                    else detail["delivery_fee"]
                )
                other_fee = (
                    _to_decimal(manual_other_fee)
                    if manual_other_fee not in (None, "")
                    else detail["other_fee"]
                )

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
            messages.success(request, "Stock In expense created successfully.")
            return redirect("batch_expense_list")
    else:
        form = BatchExpenseForm()

    return render(
        request,
        "finance/create_batch_expense.html",
        {
            "title": "Create Stock In Expense",
            "form": form,
            "back_url": "batch_expense_list",
        },
    )


@login_required
@permission_required("finance.add_expense", raise_exception=True)
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
@permission_required("finance.view_profit_dashboard_nav", raise_exception=True)
def revenue_dashboard(request):
    """Owner revenue, product mix and profit report."""
    return profit_dashboard(request)


@login_required
@permission_required("finance.view_profit_dashboard_nav", raise_exception=True)
def profit_dashboard(request):
    from orders.models import Order, OrderItem

    today = timezone.localdate()
    default_start = today.replace(day=1)
    default_end = today

    date_from_raw = request.GET.get("date_from") or ""
    date_to_raw = request.GET.get("date_to") or ""

    date_from = default_start
    date_to = default_end

    try:
        if date_from_raw:
            date_from = timezone.datetime.strptime(date_from_raw, "%Y-%m-%d").date()
    except Exception:
        date_from = default_start

    try:
        if date_to_raw:
            date_to = timezone.datetime.strptime(date_to_raw, "%Y-%m-%d").date()
    except Exception:
        date_to = default_end

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    excluded_statuses = ["CANCEL", "CANCELLED", "CANCELED", "VOID"]

    base_order_qs = Order.objects.filter(
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
        is_deleted=False,
    ).exclude(status__in=excluded_statuses)

    base_item_qs = OrderItem.objects.filter(
        order__created_at__date__gte=date_from,
        order__created_at__date__lte=date_to,
        order__is_deleted=False,
    ).exclude(order__status__in=excluded_statuses)

    base_expense_qs = Expense.objects.filter(
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )

    # Sales mix for the selected period.
    top_cloth = (
        base_item_qs.filter(shirt_item__isnull=False)
        .values("shirt_item__code", "shirt_item__name")
        .annotate(qty=Sum("quantity"), revenue=Sum("line_total"))
        .order_by("-qty", "-revenue")
        .first()
    )
    top_color = (
        base_item_qs.filter(color__isnull=False)
        .values("color__name", "color__hex_code")
        .annotate(qty=Sum("quantity"))
        .order_by("-qty")
        .first()
    )
    top_size = (
        base_item_qs.filter(size__isnull=False)
        .values("size__name")
        .annotate(qty=Sum("quantity"))
        .order_by("-qty")
        .first()
    )
    top_customer = (
        base_order_qs.values("customer_name")
        .annotate(revenue=Sum("total_amount"), orders=Count("id"))
        .order_by("-revenue", "-orders")
        .first()
    )

    def get_summary(order_type=None):
        order_qs = base_order_qs
        item_qs = base_item_qs

        if order_type:
            order_qs = order_qs.filter(order_type=order_type)
            item_qs = item_qs.filter(order__order_type=order_type)

        total_amount = order_qs.aggregate(total=Sum("total_amount"))["total"] or Decimal("0")
        deposit = order_qs.aggregate(total=Sum("deposit_amount"))["total"] or Decimal("0")
        paid = order_qs.aggregate(total=Sum("paid_amount"))["total"] or Decimal("0")
        receivable = total_amount - deposit - paid

        cloth_qs = item_qs.filter(shirt_item__isnull=False)
        cloth_sold = cloth_qs.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        cloth_revenue = cloth_qs.aggregate(total=Sum("line_total"))["total"] or Decimal("0")

        film_qs = item_qs.filter(film_item__isnull=False)
        film_sold = film_qs.aggregate(total=Sum("film_meter"))["total"] or Decimal("0")
        film_revenue = film_qs.aggregate(total=Sum("line_total"))["total"] or Decimal("0")

        return {
            "total_amount": total_amount,
            "deposit": deposit,
            "paid": paid,
            "receivable": receivable,
            "cloth_sold": cloth_sold,
            "cloth_revenue": cloth_revenue,
            "film_sold": film_sold,
            "film_revenue": film_revenue,
        }

    niron = get_summary("NIRON")
    kampu = get_summary("KAMPU")
    total = get_summary()

    expense_total = base_expense_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    profit_total = total["total_amount"] - expense_total
    total_inventory = _get_total_inventory()

    expense_by_type = {
        "other": base_expense_qs.filter(expense_type=Expense.TYPE_OTHER).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
        "batch": base_expense_qs.filter(expense_type=Expense.TYPE_BATCH).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
        "operating": base_expense_qs.filter(expense_type=Expense.TYPE_OPERATING).aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
    }

    recent_expenses = (
        base_expense_qs
        .select_related("created_by", "batch")
        .order_by("-created_at", "-id")[:8]
    )

    niron_qs = (
        base_order_qs.filter(order_type="NIRON")
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Sum("total_amount"))
        .order_by("day")
    )

    kampu_qs = (
        base_order_qs.filter(order_type="KAMPU")
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Sum("total_amount"))
        .order_by("day")
    )

    total_qs = (
        base_order_qs
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Sum("total_amount"))
        .order_by("day")
    )

    expense_qs = (
        base_expense_qs
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Sum("amount"))
        .order_by("day")
    )

    niron_map = {row["day"]: float(row["total"] or 0) for row in niron_qs}
    kampu_map = {row["day"]: float(row["total"] or 0) for row in kampu_qs}
    total_map = {row["day"]: float(row["total"] or 0) for row in total_qs}
    expense_map = {row["day"]: float(row["total"] or 0) for row in expense_qs}

    chart_labels = []
    niron_values = []
    kampu_values = []
    total_values = []
    expense_labels = []
    expense_values = []

    current = date_from
    while current <= date_to:
        label = current.strftime("%d %b")
        chart_labels.append(label)
        expense_labels.append(label)

        niron_values.append(niron_map.get(current, 0))
        kampu_values.append(kampu_map.get(current, 0))
        total_values.append(total_map.get(current, 0))
        expense_values.append(expense_map.get(current, 0))

        current += timedelta(days=1)

    # ==========================================================
    # MONTHLY BUSINESS GROWTH ANALYSIS
    # ==========================================================
    # Month inputs use YYYY-MM and can cross years. By default, show
    # the previous 3 months plus the current month.
    def month_start_from_raw(raw_value, fallback):
        try:
            return timezone.datetime.strptime(raw_value, "%Y-%m").date().replace(day=1)
        except (TypeError, ValueError):
            return fallback

    def add_months(value, months):
        month_index = (value.year * 12 + value.month - 1) + months
        return date(month_index // 12, month_index % 12 + 1, 1)

    current_month_start = today.replace(day=1)
    default_growth_from = add_months(current_month_start, -3)
    default_growth_to = current_month_start

    growth_from = month_start_from_raw(
        (request.GET.get("growth_from") or "").strip(),
        default_growth_from,
    )
    growth_to = month_start_from_raw(
        (request.GET.get("growth_to") or "").strip(),
        default_growth_to,
    )

    if growth_from > growth_to:
        growth_from, growth_to = growth_to, growth_from

    # Keep the dashboard responsive even if a very large range is entered.
    max_growth_months = 36
    if ((growth_to.year - growth_from.year) * 12 + growth_to.month - growth_from.month) >= max_growth_months:
        growth_from = add_months(growth_to, -(max_growth_months - 1))

    def growth_info(current_value, previous_value):
        current_value = _to_decimal(current_value)
        previous_value = _to_decimal(previous_value)

        if previous_value == 0:
            if current_value == 0:
                return {"display": "0.00%", "value": 0.0, "css": "same", "direction": "same"}
            return {"display": "New", "value": None, "css": "up", "direction": "up"}

        percent = ((current_value - previous_value) / abs(previous_value)) * Decimal("100")
        percent = percent.quantize(Decimal("0.01"))

        if percent > 0:
            css = "up"
            direction = "up"
            display = f"+{percent:.2f}%"
        elif percent < 0:
            css = "down"
            direction = "down"
            display = f"{percent:.2f}%"
        else:
            css = "same"
            direction = "same"
            display = "0.00%"

        return {
            "display": display,
            "value": float(percent),
            "css": css,
            "direction": direction,
        }

    service_definitions = [
        ("full", Order.SERVICE_FULL, "Full Order"),
        ("print_heatpress", Order.SERVICE_PRINT_HEATPRESS, "Print & Heat Press"),
        ("film_only", Order.SERVICE_FILM_ONLY, "Film Only"),
        ("retail", Order.SERVICE_RETAIL, "Retail Sale"),
    ]

    def build_month_snapshot(month_start):
        next_month = add_months(month_start, 1)
        month_end = next_month - timedelta(days=1)

        month_orders = Order.objects.filter(
            created_at__date__gte=month_start,
            created_at__date__lte=month_end,
            is_deleted=False,
        ).exclude(status__in=excluded_statuses)

        month_items = OrderItem.objects.filter(
            order__created_at__date__gte=month_start,
            order__created_at__date__lte=month_end,
            order__is_deleted=False,
        ).exclude(order__status__in=excluded_statuses)

        month_expenses = Expense.objects.filter(
            created_at__date__gte=month_start,
            created_at__date__lte=month_end,
        )

        revenue = month_orders.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
        expense = month_expenses.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        profit = revenue - expense
        margin = (profit / revenue * Decimal("100")) if revenue else Decimal("0.00")

        niron_orders = month_orders.filter(order_type=Order.TYPE_NIRON)
        kampu_orders = month_orders.filter(order_type=Order.TYPE_KAMPU)
        niron_revenue = niron_orders.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
        kampu_revenue = kampu_orders.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")

        cloth_items = month_items.filter(shirt_item__isnull=False)
        film_items = month_items.filter(film_item__isnull=False)
        cloth_sold = cloth_items.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        cloth_revenue = cloth_items.aggregate(total=Sum("line_total"))["total"] or Decimal("0.00")
        film_sold = film_items.aggregate(total=Sum("film_meter"))["total"] or Decimal("0")
        film_revenue = film_items.aggregate(total=Sum("line_total"))["total"] or Decimal("0.00")

        deposit = month_orders.aggregate(total=Sum("deposit_amount"))["total"] or Decimal("0.00")
        paid = month_orders.aggregate(total=Sum("paid_amount"))["total"] or Decimal("0.00")
        receivable = revenue - deposit - paid

        def service_breakdown(shop_orders):
            result = {}
            shop_total = shop_orders.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
            for key, service_code, label in service_definitions:
                service_qs = shop_orders.filter(service_type=service_code)
                service_revenue = service_qs.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
                service_count = service_qs.count()
                result[key] = {
                    "key": key,
                    "label": label,
                    "projects": service_count,
                    "revenue": service_revenue,
                    "average": (service_revenue / service_count) if service_count else Decimal("0.00"),
                    "share": (service_revenue / shop_total * Decimal("100")) if shop_total else Decimal("0.00"),
                }
            return result

        return {
            "month_date": month_start,
            "month_name": month_start.strftime("%b %Y"),
            "month_full_name": month_start.strftime("%B %Y"),
            "revenue": revenue,
            "expense": expense,
            "profit": profit,
            "margin": margin,
            "orders": month_orders.count(),
            "niron_projects": niron_orders.count(),
            "kampu_projects": kampu_orders.count(),
            "niron_revenue": niron_revenue,
            "kampu_revenue": kampu_revenue,
            "niron_average": (niron_revenue / niron_orders.count()) if niron_orders.exists() else Decimal("0.00"),
            "kampu_average": (kampu_revenue / kampu_orders.count()) if kampu_orders.exists() else Decimal("0.00"),
            "cloth_sold": cloth_sold,
            "cloth_revenue": cloth_revenue,
            "film_sold": film_sold,
            "film_revenue": film_revenue,
            "paid": paid,
            "receivable": receivable,
            "niron_services": service_breakdown(niron_orders),
            "kampu_services": service_breakdown(kampu_orders),
        }

    # Load one month before the selected range so the first visible month has
    # a real comparison percentage.
    previous_snapshot = build_month_snapshot(add_months(growth_from, -1))
    monthly_rows = []
    cursor = growth_from

    growth_metrics = [
        "revenue", "expense", "profit", "orders",
        "niron_projects", "kampu_projects",
        "niron_revenue", "kampu_revenue",
        "cloth_sold", "cloth_revenue",
        "film_sold", "film_revenue", "paid", "receivable",
    ]

    while cursor <= growth_to:
        row = build_month_snapshot(cursor)
        row["growth"] = {
            metric: growth_info(row[metric], previous_snapshot[metric])
            for metric in growth_metrics
        }

        for shop_key in ("niron_services", "kampu_services"):
            for service_key, _, _ in service_definitions:
                current_service = row[shop_key][service_key]
                previous_service = previous_snapshot[shop_key][service_key]
                current_service["revenue_growth"] = growth_info(
                    current_service["revenue"], previous_service["revenue"]
                )
                current_service["project_growth"] = growth_info(
                    current_service["projects"], previous_service["projects"]
                )

        monthly_rows.append(row)
        previous_snapshot = row
        cursor = add_months(cursor, 1)

    # Totals for an easy shop/service overview across the selected range.
    selected_end_exclusive = add_months(growth_to, 1)
    selected_orders = Order.objects.filter(
        created_at__date__gte=growth_from,
        created_at__date__lt=selected_end_exclusive,
        is_deleted=False,
    ).exclude(status__in=excluded_statuses)

    def build_service_summary(order_type):
        shop_orders = selected_orders.filter(order_type=order_type)
        shop_revenue = shop_orders.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
        rows = []
        for key, service_code, label in service_definitions:
            service_qs = shop_orders.filter(service_type=service_code)
            service_revenue = service_qs.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
            project_count = service_qs.count()
            rows.append({
                "key": key,
                "label": label,
                "projects": project_count,
                "revenue": service_revenue,
                "average": (service_revenue / project_count) if project_count else Decimal("0.00"),
                "share": (service_revenue / shop_revenue * Decimal("100")) if shop_revenue else Decimal("0.00"),
            })
        return {
            "projects": shop_orders.count(),
            "revenue": shop_revenue,
            "average": (shop_revenue / shop_orders.count()) if shop_orders.exists() else Decimal("0.00"),
            "services": rows,
        }

    niron_service_summary = build_service_summary(Order.TYPE_NIRON)
    kampu_service_summary = build_service_summary(Order.TYPE_KAMPU)

    monthly_chart_labels = [row["month_name"] for row in monthly_rows]
    monthly_revenue_values = [float(row["revenue"]) for row in monthly_rows]
    monthly_expense_values = [float(row["expense"]) for row in monthly_rows]
    monthly_profit_values = [float(row["profit"]) for row in monthly_rows]
    monthly_niron_values = [float(row["niron_revenue"]) for row in monthly_rows]
    monthly_kampu_values = [float(row["kampu_revenue"]) for row in monthly_rows]
    monthly_niron_project_values = [row["niron_projects"] for row in monthly_rows]
    monthly_kampu_project_values = [row["kampu_projects"] for row in monthly_rows]
    monthly_revenue_growth_values = [
        row["growth"]["revenue"]["value"] if row["growth"]["revenue"]["value"] is not None else 0
        for row in monthly_rows
    ]
    monthly_profit_growth_values = [
        row["growth"]["profit"]["value"] if row["growth"]["profit"]["value"] is not None else 0
        for row in monthly_rows
    ]

    niron_service_chart = {
        key: [float(row["niron_services"][key]["revenue"]) for row in monthly_rows]
        for key, _, _ in service_definitions
    }
    kampu_service_chart = {
        key: [float(row["kampu_services"][key]["revenue"]) for row in monthly_rows]
        for key, _, _ in service_definitions
    }

    growth_range_label = (
        growth_from.strftime("%b %Y")
        if growth_from == growth_to
        else f"{growth_from.strftime('%b %Y')} – {growth_to.strftime('%b %Y')}"
    )

    return render(
        request,
        "finance/profit_dashboard.html",
        {
            "niron": niron,
            "kampu": kampu,
            "total": total,
            "expense_total": expense_total,
            "profit_total": profit_total,
            "total_inventory": total_inventory,
        "top_cloth": top_cloth,
        "top_color": top_color,
        "top_size": top_size,
        "top_customer": top_customer,
            "expense_by_type": expense_by_type,
            "recent_expenses": recent_expenses,

            "chart_labels": chart_labels,
            "niron_values": niron_values,
            "kampu_values": kampu_values,
            "total_values": total_values,
            "expense_labels": expense_labels,
            "expense_values": expense_values,

            "date_from": date_from,
            "date_to": date_to,

            "growth_from": growth_from,
            "growth_to": growth_to,
            "growth_range_label": growth_range_label,
            "monthly_rows": monthly_rows,
            "niron_service_summary": niron_service_summary,
            "kampu_service_summary": kampu_service_summary,
            "monthly_chart_labels": monthly_chart_labels,
            "monthly_revenue_values": monthly_revenue_values,
            "monthly_expense_values": monthly_expense_values,
            "monthly_profit_values": monthly_profit_values,
            "monthly_niron_values": monthly_niron_values,
            "monthly_kampu_values": monthly_kampu_values,
            "monthly_niron_project_values": monthly_niron_project_values,
            "monthly_kampu_project_values": monthly_kampu_project_values,
            "monthly_revenue_growth_values": monthly_revenue_growth_values,
            "monthly_profit_growth_values": monthly_profit_growth_values,
            "niron_service_chart": niron_service_chart,
            "kampu_service_chart": kampu_service_chart,
        },
    )
@login_required
@permission_required("finance.add_expense", raise_exception=True)
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


@login_required
@permission_required("finance.view_expense_summary_nav", raise_exception=True)
def expense_summary_export_excel(request):
    qs = Expense.objects.select_related("created_by", "batch").all()
    form, qs = _apply_filters(request, qs)
    qs = qs.order_by("-created_at", "-id")

    wb = Workbook()
    ws = wb.active
    ws.title = "Expense Summary"

    ws.append([
        "Date",
        "Type",
        "Title",
        "Amount",
        "Record By",
        "Note",
    ])

    for row in qs:
        if row.expense_type == Expense.TYPE_BATCH and row.batch:
            title = row.batch.batch_no
        elif row.expense_type == Expense.TYPE_OPERATING:
            title = row.get_category_display() if hasattr(row, "get_category_display") else (row.category or "")
        else:
            title = "Other Expense"

        record_by = ""
        if row.created_by:
            record_by = row.created_by.get_full_name() or row.created_by.username

        ws.append([
            row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
            row.get_expense_type_display(),
            title,
            float(row.amount or 0),
            record_by,
            row.note or "",
        ])

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="expense_summary.xlsx"'

    wb.save(response)
    return response

@login_required
@permission_required("finance.add_expense", raise_exception=True)
def batch_expense_cost_edit(request, pk):
    expense = get_object_or_404(
        Expense.objects.select_related(
            "batch",
            "batch__created_by",
            "created_by",
        ),
        pk=pk,
        expense_type=Expense.TYPE_BATCH,
    )

    item_rows = []
    total_received_qty = Decimal("0")
    stock_recorded_by = None
    summary_type = "Inventory"
    summary_unit = "units"

    if expense.batch_id:
        stock_recorded_by = expense.batch.created_by

        rows = expense.batch.items.select_related(
            "item",
            "color",
            "size",
        ).filter(is_active=True)

        note_lower = (expense.note or "").lower()
        if "cloth" in note_lower:
            summary_type = "Cloth"
            summary_unit = "shirts"
        elif "printing" in note_lower:
            summary_type = "Printing Material"
            summary_unit = "units"
        else:
            # Detect from the first available item type.
            first_row = rows.first()
            if (
                first_row
                and first_row.item
                and first_row.item.item_type == InventoryItem.TYPE_SHIRT
            ):
                summary_type = "Cloth"
                summary_unit = "shirts"
            else:
                summary_type = "Printing Material"
                summary_unit = "units"

        for row in rows:
            qty = Decimal(row.qty_received or 0)
            total_received_qty += qty

            image_url = ""
            if row.item and getattr(row.item, "image", None):
                try:
                    image_url = row.item.image.url
                except (ValueError, AttributeError):
                    image_url = ""

            item_rows.append(
                {
                    "item_name": row.item.name if row.item else "-",
                    "item_code": row.item.code if row.item else "",
                    "color_name": row.color.name if row.color else "-",
                    "size_name": row.size.name if row.size else "-",
                    "quantity": qty,
                    "unit_label": (
                        "shirt"
                        if summary_type == "Cloth" and qty == 1
                        else "shirts"
                        if summary_type == "Cloth"
                        else "unit"
                        if qty == 1
                        else "units"
                    ),
                    "image_url": image_url,
                    "summary_type": summary_type,
                }
            )

    elif (
        expense.stock_source_type == Expense.SOURCE_FABRIC
        and expense.fabric_receipt_ids
    ):
        summary_type = "Fabric"
        summary_unit = "rolls"

        receipts = FabricReceipt.objects.select_related(
            "fabric_type",
            "color",
            "created_by",
        ).filter(pk__in=expense.fabric_receipt_ids)

        first_receipt = receipts.first()
        stock_recorded_by = first_receipt.created_by if first_receipt else None

        for receipt in receipts:
            qty = Decimal(receipt.roll_count or 0)
            total_received_qty += qty

            image_url = ""
            fabric_type = receipt.fabric_type if receipt.fabric_type_id else None
            if fabric_type and getattr(fabric_type, "image", None):
                try:
                    image_url = fabric_type.image.url
                except (ValueError, AttributeError):
                    image_url = ""

            item_rows.append(
                {
                    "item_name": (
                        fabric_type.name
                        if fabric_type
                        else receipt.fabric_name
                    ),
                    "item_code": receipt.receipt_no,
                    "color_name": (
                        receipt.color.name if receipt.color_id else "-"
                    ),
                    "size_name": "-",
                    "quantity": qty,
                    "unit_label": "roll" if qty == 1 else "rolls",
                    "image_url": image_url,
                    "summary_type": "Fabric",
                }
            )

    if request.method == "POST":
        form = BatchExpenseCostForm(request.POST, instance=expense)

        if form.is_valid():
            obj = form.save(commit=False)
            obj.amount = (
                (obj.batch_cost or Decimal("0"))
                + (obj.batch_delivery_fee or Decimal("0"))
                + (obj.batch_other_fee or Decimal("0"))
            )
            obj.expense_status = Expense.STATUS_COMPLETED
            obj.created_by = request.user
            obj.save()

            messages.success(
                request,
                "Stock In expense cost saved successfully.",
            )
            return redirect("batch_expense_list")
    else:
        form = BatchExpenseCostForm(instance=expense)

    current_total = (
        (expense.batch_cost or Decimal("0"))
        + (expense.batch_delivery_fee or Decimal("0"))
        + (expense.batch_other_fee or Decimal("0"))
    )

    average_cost = (
        current_total / total_received_qty
        if total_received_qty > 0
        else Decimal("0")
    )

    cost_recorded_by = (
        expense.created_by
        if expense.expense_status == Expense.STATUS_COMPLETED
        else None
    )

    return render(
        request,
        "finance/batch_expense_cost_form.html",
        {
            "expense": expense,
            "form": form,
            "item_rows": item_rows,
            "total_received_qty": total_received_qty,
            "average_cost": average_cost,
            "stock_recorded_by": stock_recorded_by,
            "cost_recorded_by": cost_recorded_by,
            "summary_type": summary_type,
            "summary_unit": summary_unit,
        },
    )