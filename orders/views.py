from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Prefetch, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from customers.models import Customer
from decimal import Decimal, ROUND_HALF_UP
from inventory.models import Color, InventoryItem, Size
from openpyxl import Workbook

from django.template.loader import render_to_string
from playwright.sync_api import sync_playwright

from .forms import OrderForm, ProductionFilterForm
from .models import (
    Order,
    OrderDesign,
    OrderDesignFile,
    OrderHistory,
    OrderItem,
    OrderProgress,
)
from .services import restore_stock_for_order, deduct_stock_for_order

import re


def _safe_download_name(value, fallback="file"):
    value = str(value or "").strip()
    value = re.sub(r'[\\/*?:"<>|]+', "", value)
    value = re.sub(r"\s+", "_", value)
    return value or fallback

def _stringify(value):
    if value is None:
        return ""
    return str(value)


def _decimal_or_zero(value):
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def _log_order_history(order, action, field_name="", old_value="", new_value="", user=None, remark=""):
    OrderHistory.objects.create(
        order=order,
        action=action,
        field_name=field_name,
        old_value=_stringify(old_value),
        new_value=_stringify(new_value),
        changed_by=user if user and user.is_authenticated else None,
        remark=remark or "",
    )


def _snapshot_order(order):
    return {
        "order_type": order.order_type,
        "service_type": order.service_type,
        "customer_name": order.customer_name,
        "phone": order.phone,
        "customer_location": order.customer_location,
        "deadline": order.deadline.isoformat() if order.deadline else "",
        "remark": order.remark,
        "total_amount": str(order.total_amount or 0),
        "deposit_amount": str(order.deposit_amount or 0),
        "paid_amount": str(order.paid_amount or 0),
        "status": order.status,
        "total_pcs": str(order.total_pcs or 0),
        "done_pcs": str(order.done_pcs or 0),
    }


def _snapshot_item(item):
    return {
        "design": item.design.display_name if getattr(item, "design", None) else "",
        "description": item.description,
        "shirt_item": str(item.shirt_item) if item.shirt_item else "",
        "film_item": str(item.film_item) if item.film_item else "",
        "color": str(item.color) if item.color else "",
        "size": str(item.size) if item.size else "",
        "quantity": str(item.quantity or 0),
        "done_qty": str(item.done_qty or 0),
        "unit_price": str(item.unit_price or 0),
        "film_meter": str(item.film_meter or 0),
        "line_total": str(item.line_total or 0),
    }


def _snapshot_design(design):
    return {
        "name": design.name,
        "remark": design.remark,
        "sort_order": design.sort_order,
    }


def _log_order_changes(order, before_data, after_data, user=None):
    for field_name, old_value in before_data.items():
        new_value = after_data.get(field_name)
        if _stringify(old_value) != _stringify(new_value):
            _log_order_history(
                order=order,
                action=OrderHistory.ACTION_EDIT,
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
                user=user,
            )


def _get_cancel_status():
    if hasattr(Order, "STATUS_CANCEL"):
        return Order.STATUS_CANCEL
    if hasattr(Order, "STATUS_CANCELLED"):
        return Order.STATUS_CANCELLED
    return "CANCEL"


def _status_badge(status):
    cancel_status = _get_cancel_status()

    if status == Order.STATUS_DONE:
        return "green"
    if status == cancel_status:
        return "red"
    if status == Order.STATUS_PROCESSING:
        return "blue"
    return "yellow"


def _format_countdown(deadline):
    if not deadline:
        return "-"

    today = timezone.localdate()

    if deadline < today:
        return "Overdue"

    diff_days = (deadline - today).days

    if diff_days == 0:
        return "Today"

    if diff_days == 1:
        return "1day"

    return f"{diff_days}days"


def _get_prefetched_order_queryset():
    return Order.objects.prefetch_related(
        Prefetch(
            "designs",
            queryset=OrderDesign.objects.prefetch_related(
                Prefetch(
                    "items",
                    queryset=OrderItem.objects.select_related(
                        "shirt_item",
                        "film_item",
                        "color",
                        "size",
                        "design",
                        "order",
                    ).prefetch_related("progress_logs"),
                ),
                "files",
            ).order_by("sort_order", "id"),
        ),
        "items",
        "design_files",
        "history_logs",
        "progress_logs__order_item__design",
        "stock_consumptions__batch_item__item",
    )


def _order_form_context_base():
    return {
        "shirt_items": InventoryItem.objects.filter(
            item_type=InventoryItem.TYPE_SHIRT,
            is_active=True,
        ).order_by("code", "name"),
        "film_items": InventoryItem.objects.filter(
            item_type=InventoryItem.TYPE_FILM,
            is_active=True,
        ).order_by("code", "name"),
        "colors": Color.objects.filter(is_active=True).order_by("name"),
        "sizes": Size.objects.filter(is_active=True).order_by("sort_order", "id"),
    }


def _build_design_payloads_from_post(request):
    payloads = []
    design_total = int(request.POST.get("design_total", 0) or 0)

    for design_index in range(design_total):
        prefix = f"design-{design_index}"

        design_id = request.POST.get(f"{prefix}-id") or ""
        design_name = (request.POST.get(f"{prefix}-name") or "").strip()
        design_remark = (request.POST.get(f"{prefix}-remark") or "").strip()
        delete_design = request.POST.get(f"{prefix}-DELETE") == "1"
        item_total = int(request.POST.get(f"{prefix}-item_total", 0) or 0)

        items = []

        for item_index in range(item_total):
            item_prefix = f"{prefix}-item-{item_index}"
            delete_item = request.POST.get(f"{item_prefix}-DELETE") == "1"

            qty = _decimal_or_zero(
                request.POST.get(f"{item_prefix}-quantity")
            ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

            items.append(
                {
                    "id": request.POST.get(f"{item_prefix}-id") or "",
                    "description": (request.POST.get(f"{item_prefix}-description") or "").strip(),
                    "shirt_item_id": request.POST.get(f"{item_prefix}-shirt_item") or None,
                    "film_item_id": request.POST.get(f"{item_prefix}-film_item") or None,
                    "color_id": request.POST.get(f"{item_prefix}-color") or None,
                    "size_id": request.POST.get(f"{item_prefix}-size") or None,
                    "quantity": qty,
                    "unit_price": _decimal_or_zero(request.POST.get(f"{item_prefix}-unit_price")),
                    "film_meter": _decimal_or_zero(request.POST.get(f"{item_prefix}-film_meter")),
                    "delete": delete_item,
                }
            )

        payloads.append(
            {
                "id": design_id,
                "name": design_name,
                "remark": design_remark,
                "delete": delete_design,
                "files": request.FILES.getlist(f"{prefix}-design_files"),
                "items": items,
            }
        )

    return payloads


def _save_design_payloads(order, design_payloads, user=None, is_edit=False):
    total_amount = Decimal("0")
    total_pcs = Decimal("0")

    existing_designs = {str(d.pk): d for d in order.designs.all()}
    existing_items = {str(i.pk): i for i in order.items.all()}

    kept_design_ids = set()
    kept_item_ids = set()
    next_sort_order = 1

    for design_data in design_payloads:
        design_id = design_data["id"]
        delete_design = design_data["delete"]

        if design_id and design_id in existing_designs:
            design = existing_designs[design_id]
            before_design = _snapshot_design(design)
        else:
            design = None
            before_design = None

        if delete_design:
            if design:
                _log_order_history(
                    order=order,
                    action=OrderHistory.ACTION_ITEM_DELETE,
                    field_name=f"design#{design.pk}",
                    old_value=before_design,
                    new_value="",
                    user=user,
                    remark=f"Design removed: {design.display_name}",
                )
                design.delete()
            continue

        if design is None:
            design = OrderDesign.objects.create(
                order=order,
                name=design_data["name"],
                remark=design_data["remark"],
                sort_order=next_sort_order,
            )
            _log_order_history(
                order=order,
                action=OrderHistory.ACTION_DESIGN_ADD,
                field_name="design",
                old_value="",
                new_value=_snapshot_design(design),
                user=user,
                remark=f"Design created: {design.display_name}",
            )
        else:
            design.name = design_data["name"]
            design.remark = design_data["remark"]
            design.sort_order = next_sort_order
            design.save(update_fields=["name", "remark", "sort_order"])

        kept_design_ids.add(str(design.pk))
        next_sort_order += 1
        has_item_or_file = False

        for item_data in design_data["items"]:
            item_id = item_data["id"]
            delete_item = item_data["delete"]

            if item_id and item_id in existing_items:
                item = existing_items[item_id]
                old_item_data = _snapshot_item(item)
            else:
                item = None
                old_item_data = None

            if delete_item:
                if item:
                    _log_order_history(
                        order=order,
                        action=OrderHistory.ACTION_ITEM_DELETE,
                        field_name=f"item#{item.pk}",
                        old_value=old_item_data,
                        new_value="",
                        user=user,
                        remark=f"Item removed from {design.display_name}",
                    )
                    item.delete()
                continue

            is_blank_item = (
                not item_data["description"]
                and not item_data["shirt_item_id"]
                and not item_data["film_item_id"]
                and item_data["quantity"] <= 0
                and item_data["unit_price"] <= 0
                and item_data["film_meter"] <= 0
            )

            if is_blank_item:
                continue

            if order.service_type == Order.SERVICE_FULL:
                if (
                    not item_data["shirt_item_id"]
                    or not item_data["color_id"]
                    or not item_data["size_id"]
                    or item_data["quantity"] <= 0
                    or item_data["unit_price"] <= 0
                ):
                    raise ValidationError(
                        "Full Order requires Shirt Item, Color, Size, Qty, and Unit Price."
                    )

                item_data["quantity"] = item_data["quantity"].quantize(Decimal("1"))
                item_data["film_item_id"] = None
                item_data["film_meter"] = Decimal("0")

            elif order.service_type == Order.SERVICE_FILM_ONLY:
                if (
                    not item_data["film_item_id"]
                    or item_data["film_meter"] <= 0
                    or item_data["unit_price"] <= 0
                ):
                    raise ValidationError(
                        "Film Only requires Film Item, Film Meter, and Unit Price."
                    )

                item_data["shirt_item_id"] = None
                item_data["color_id"] = None
                item_data["size_id"] = None
                item_data["quantity"] = Decimal("0")

            elif order.service_type == Order.SERVICE_PRINT_HEATPRESS:
                if item_data["quantity"] <= 0 or item_data["unit_price"] <= 0:
                    raise ValidationError(
                        "Print & Heat Press requires Qty and Unit Price."
                    )

                item_data["quantity"] = item_data["quantity"].quantize(Decimal("1"))
                item_data["shirt_item_id"] = None
                item_data["film_item_id"] = None
                item_data["color_id"] = None
                item_data["size_id"] = None
                item_data["film_meter"] = Decimal("0")

            if item is None:
                item = OrderItem.objects.create(
                    order=order,
                    design=design,
                    description=item_data["description"],
                    shirt_item_id=item_data["shirt_item_id"],
                    film_item_id=item_data["film_item_id"],
                    color_id=item_data["color_id"],
                    size_id=item_data["size_id"],
                    quantity=item_data["quantity"],
                    unit_price=item_data["unit_price"],
                    film_meter=item_data["film_meter"],
                )

                _log_order_history(
                    order=order,
                    action=OrderHistory.ACTION_ITEM_ADD,
                    field_name="item",
                    old_value="",
                    new_value=_snapshot_item(item),
                    user=user,
                    remark=f"Item added in {design.display_name}",
                )
            else:
                item.order = order
                item.design = design
                item.description = item_data["description"]
                item.shirt_item_id = item_data["shirt_item_id"]
                item.film_item_id = item_data["film_item_id"]
                item.color_id = item_data["color_id"]
                item.size_id = item_data["size_id"]
                item.quantity = item_data["quantity"]
                item.unit_price = item_data["unit_price"]
                item.film_meter = item_data["film_meter"]
                item.save()

                new_item_data = _snapshot_item(item)
                if old_item_data:
                    for key, old_val in old_item_data.items():
                        new_val = new_item_data.get(key)
                        if _stringify(old_val) != _stringify(new_val):
                            _log_order_history(
                                order=order,
                                action=OrderHistory.ACTION_ITEM_EDIT,
                                field_name=f"item#{item.pk}.{key}",
                                old_value=old_val,
                                new_value=new_val,
                                user=user,
                                remark=f"Item updated in {design.display_name}",
                            )

            kept_item_ids.add(str(item.pk))
            has_item_or_file = True
            total_amount += Decimal(item.line_total or 0)

            if order.service_type == Order.SERVICE_FILM_ONLY:
                total_pcs += Decimal("0")
            else:
                total_pcs += Decimal(item.quantity or 0)

        for f in design_data["files"]:
            OrderDesignFile.objects.create(
                order=order,
                design=design,
                image=f,
            )
            has_item_or_file = True

        if not has_item_or_file:
            design.delete()
            kept_design_ids.discard(str(design.pk))

    if is_edit:
        for item in list(order.items.all()):
            if str(item.pk) not in kept_item_ids:
                item.delete()

        for design in list(order.designs.all()):
            if str(design.pk) not in kept_design_ids:
                design.delete()

    return total_amount, total_pcs


def _get_order_totals_by_service(order):
    if order.service_type == Order.SERVICE_FULL:
        cloth_qty = order.items.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        film_meter = Decimal("0")
    elif order.service_type == Order.SERVICE_FILM_ONLY:
        cloth_qty = Decimal("0")
        film_meter = order.items.aggregate(total=Sum("film_meter"))["total"] or Decimal("0")
    else:
        cloth_qty = Decimal("0")
        film_meter = order.items.aggregate(total=Sum("film_meter"))["total"] or Decimal("0")

    return cloth_qty, film_meter


def _get_or_create_customer_from_request(request):
    customer_name = (request.POST.get("customer_name") or "").strip()
    phone = (request.POST.get("phone") or "").strip()
    location = (request.POST.get("customer_location") or "").strip()

    if not customer_name:
        return None

    customer, created = Customer.objects.get_or_create(
        name=customer_name,
        defaults={
            "phone": phone,
            "location": location,
        },
    )

    if not created:
        changed = False

        if phone and customer.phone != phone:
            customer.phone = phone
            changed = True

        if location and customer.location != location:
            customer.location = location
            changed = True

        if changed:
            customer.save(update_fields=["phone", "location", "updated_at"])

    return customer


@login_required
@permission_required("orders.view_order", raise_exception=True)
def order_list(request):
    keyword = (request.GET.get("keyword") or "").strip()
    status = (request.GET.get("status") or "").strip()
    order_type = (request.GET.get("order_type") or "").strip()
    created_date_from = (request.GET.get("created_date_from") or "").strip()
    created_date_to = (request.GET.get("created_date_to") or "").strip()
    show_trash = request.GET.get("trash") == "1"

    qs = Order.objects.all().prefetch_related("items")

    if show_trash:
        qs = qs.filter(is_deleted=True)
    else:
        qs = qs.filter(is_deleted=False)

    qs = qs.order_by("-created_at", "-id")

    if keyword:
        qs = qs.filter(
            Q(order_no__icontains=keyword)
            | Q(customer_name__icontains=keyword)
            | Q(phone__icontains=keyword)
            | Q(customer_location__icontains=keyword)
            | Q(remark__icontains=keyword)
        )

    if status:
        qs = qs.filter(status=status)

    if order_type:
        qs = qs.filter(order_type=order_type)

    if created_date_from:
        qs = qs.filter(created_at__date__gte=created_date_from)

    if created_date_to:
        qs = qs.filter(created_at__date__lte=created_date_to)

    rows = []
    total_orders = qs.count()
    pending_count = qs.filter(status=Order.STATUS_PENDING).count()
    done_count = qs.filter(status=Order.STATUS_DONE).count()

    # ✅ FIX: use DATE instead of datetime
    today = timezone.localdate()

    for order in qs:
        cloth_qty, film_meter = _get_order_totals_by_service(order)

        total_amount = Decimal(order.total_amount or 0)
        paid_amount = Decimal(order.paid_amount or 0)
        deposit_amount = Decimal(order.deposit_amount or 0)
        balance_amount = total_amount - deposit_amount - paid_amount

        # ✅ FIX: compare date with date (no error anymore)
        is_late = bool(
            order.deadline
            and order.status != Order.STATUS_DONE
            and order.deadline < today
        )

        rows.append(
            {
                "obj": order,
                "cloth_qty": cloth_qty,
                "film_meter": film_meter,
                "balance_amount": balance_amount,
                "is_late": is_late,
            }
        )

    context = {
        "rows": rows,
        "keyword": keyword,
        "status": status,
        "order_type": order_type,
        "created_date_from": created_date_from,
        "created_date_to": created_date_to,
        "show_trash": show_trash,
        "total_orders": total_orders,
        "pending_count": pending_count,
        "done_count": done_count,
        "status_choices": getattr(Order, "STATUS_CHOICES", []),
        "order_type_choices": getattr(Order, "TYPE_CHOICES", []),
    }

    return render(request, "orders/order_list.html", context)

@login_required
@permission_required("orders.view_order", raise_exception=True)
def production_list(request):
    data = request.GET.copy()

    if "status" not in data:
        data["status"] = ProductionFilterForm.STATUS_ACTIVE

    if "sort" not in data:
        data["sort"] = ProductionFilterForm.SORT_DEADLINE_ASC

    form = ProductionFilterForm(data)

    qs = (
        Order.objects
        .filter(is_deleted=False)
        .prefetch_related("items")
    )

    if form.is_valid():
        keyword = (form.cleaned_data.get("q") or "").strip()
        status = form.cleaned_data.get("status") or ProductionFilterForm.STATUS_ACTIVE
        deadline = form.cleaned_data.get("deadline")
        sort = form.cleaned_data.get("sort") or ProductionFilterForm.SORT_DEADLINE_ASC

        if keyword:
            qs = qs.filter(
                Q(customer_name__icontains=keyword) |
                Q(order_no__icontains=keyword)
            )

        cancel_status = _get_cancel_status()

        if status == ProductionFilterForm.STATUS_ACTIVE:
            qs = qs.filter(status__in=[Order.STATUS_PENDING, Order.STATUS_PROCESSING])
        elif status == ProductionFilterForm.STATUS_DONE:
            qs = qs.filter(status=Order.STATUS_DONE)
        elif status == ProductionFilterForm.STATUS_CANCEL:
            qs = qs.filter(status=cancel_status)

        if deadline:
            qs = qs.filter(deadline__date=deadline)

        if sort == ProductionFilterForm.SORT_CREATED_DESC:
            qs = qs.order_by("-created_at", "-id")
        elif sort == ProductionFilterForm.SORT_CREATED_ASC:
            qs = qs.order_by("created_at", "id")
        elif sort == ProductionFilterForm.SORT_DEADLINE_DESC:
            qs = qs.order_by("-deadline", "-id")
        else:
            qs = qs.order_by("deadline", "id")
    else:
        qs = qs.filter(
            status__in=[Order.STATUS_PENDING, Order.STATUS_PROCESSING]
        ).order_by("deadline", "id")

    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    start_no = (page_obj.number - 1) * paginator.per_page

    rows = []

    total_project_pending = qs.count()
    total_cloth_done = Decimal("0")
    total_cloth_pending = Decimal("0")

    for idx, order in enumerate(page_obj.object_list, start=1):
        total_qty = sum(Decimal(item.quantity or 0) for item in order.items.all())
        done_qty = sum(Decimal(item.done_qty or 0) for item in order.items.all())

        remaining_qty = total_qty - done_qty
        if remaining_qty < 0:
            remaining_qty = Decimal("0")

        total_cloth_done += done_qty
        total_cloth_pending += remaining_qty

        rows.append(
            {
                "no": start_no + idx,
                "order": order,
                "countdown_text": _format_countdown(order.deadline),
                "status_color": _status_badge(order.status),
                "total_qty": total_qty,
                "done_qty": done_qty,
            }
        )

    return render(
        request,
        "orders/production_list.html",
        {
            "form": form,
            "page_obj": page_obj,
            "rows": rows,
            "total_found": paginator.count,
            "total_project_pending": total_project_pending,
            "total_cloth_pending": total_cloth_pending,
            "total_cloth_done": total_cloth_done,
            "now": timezone.now(),
        },
    )
@login_required
@permission_required("orders.view_order", raise_exception=True)
def order_list_export_excel(request):
    qs = Order.objects.filter(is_deleted=False).prefetch_related("items").order_by("-created_at", "-id")

    wb = Workbook()
    ws = wb.active
    ws.title = "Orders"

    ws.append([
        "Order No", "Created At", "Customer", "Phone", "Location",
        "Order Type", "Service Type", "Status", "Cloth Qty", "Film Meter",
        "Total Amount", "Deposit Amount", "Paid Amount", "Balance", "Deadline", "Remark",
    ])

    for order in qs:
        cloth_qty, film_meter = _get_order_totals_by_service(order)
        total_amount = Decimal(order.total_amount or 0)
        deposit_amount = Decimal(order.deposit_amount or 0)
        paid_amount = Decimal(order.paid_amount or 0)
        balance_amount = total_amount - deposit_amount - paid_amount

        ws.append([
            order.order_no,
            order.created_at.strftime("%Y-%m-%d %H:%M") if order.created_at else "",
            order.customer_name or "",
            order.phone or "",
            order.customer_location or "",
            order.get_order_type_display(),
            order.get_service_type_display(),
            order.get_status_display(),
            float(cloth_qty),
            float(film_meter),
            float(total_amount),
            float(deposit_amount),
            float(paid_amount),
            float(balance_amount),
            order.deadline.strftime("%Y-%m-%d %H:%M") if order.deadline else "",
            order.remark or "",
        ])

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="orders_filtered.xlsx"'
    wb.save(response)
    return response


@login_required
@permission_required("orders.add_order", raise_exception=True)
def order_create(request):
    if request.method == "POST":
        form = OrderForm(request.POST, request.FILES)

        if form.is_valid():
            try:
                with transaction.atomic():
                    order = form.save(commit=False)
                    order.status = Order.STATUS_PENDING
                    order.customer = _get_or_create_customer_from_request(request)

                    if request.user.is_authenticated and not order.created_by_id:
                        order.created_by = request.user

                    order.save()

                    design_payloads = _build_design_payloads_from_post(request)

                    total_amount, total_pcs = _save_design_payloads(
                        order=order,
                        design_payloads=design_payloads,
                        user=request.user,
                        is_edit=False,
                    )

                    discount_amount = _decimal_or_zero(request.POST.get("discount_amount"))
                    shipping_fee = _decimal_or_zero(request.POST.get("shipping_fee"))
                    deposit_amount = _decimal_or_zero(request.POST.get("deposit_amount"))
                    paid_amount = _decimal_or_zero(request.POST.get("paid_amount"))

                    order.total_amount = total_amount - discount_amount + shipping_fee
                    order.deposit_amount = deposit_amount
                    order.paid_amount = paid_amount
                    order.total_pcs = total_pcs
                    order.done_pcs = Decimal("0")
                    order.status = Order.STATUS_PENDING
                    order.stock_deducted = False

                    order.save(update_fields=[
                        "customer",
                        "total_amount",
                        "deposit_amount",
                        "paid_amount",
                        "total_pcs",
                        "done_pcs",
                        "status",
                        "stock_deducted",
                        "created_by",
                    ])

                    deduct_stock_for_order(order, allow_shortage=True)

                    _log_order_history(
                        order=order,
                        action=OrderHistory.ACTION_CREATE,
                        field_name="order",
                        old_value="",
                        new_value=order.order_no,
                        user=request.user,
                        remark="Order created. Cloth stock deducted automatically. Film was not deducted.",
                    )

                messages.success(
                    request,
                    f"Order {order.order_no} created successfully. Cloth stock deducted automatically. Film was not deducted."
                )
                return redirect("order_detail", pk=order.pk)

            except ValidationError as e:
                messages.error(request, e.messages[0] if hasattr(e, "messages") else str(e))
        else:
            messages.error(request, "Please fix the errors below and try again.")
    else:
        form = OrderForm()

    return render(
        request,
        "orders/order_form.html",
        {
            "form": form,
            "is_edit": False,
            "submit_label": "Save Order",
            **_order_form_context_base(),
        },
    )


@login_required
@permission_required("orders.view_order", raise_exception=True)
def order_detail(request, pk):
    order = get_object_or_404(_get_prefetched_order_queryset(), pk=pk)

    total_pcs = order.items.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    done_pcs = order.items.aggregate(total=Sum("done_qty"))["total"] or Decimal("0")

    remaining_pcs = total_pcs - done_pcs
    if remaining_pcs < 0:
        remaining_pcs = Decimal("0")

    return render(
        request,
        "orders/order_detail.html",
        {
            "order": order,
            "total_pcs": total_pcs,
            "done_pcs": done_pcs,
            "remaining_pcs": remaining_pcs,
        },
    )

@login_required
@permission_required("orders.view_order", raise_exception=True)
def production_detail(request, pk):
    order = get_object_or_404(_get_prefetched_order_queryset(), pk=pk, is_deleted=False)

    # ✅ ALWAYS calculate from items
    total_pcs = order.items.aggregate(total=Sum("quantity"))["total"] or Decimal("0")
    done_pcs = order.items.aggregate(total=Sum("done_qty"))["total"] or Decimal("0")

    remaining_pcs = total_pcs - done_pcs
    if remaining_pcs < 0:
        remaining_pcs = Decimal("0")

    return render(
        request,
        "orders/production_detail.html",
        {
            "order": order,
            "total_pcs": total_pcs,
            "done_pcs": done_pcs,
            "remaining_pcs": remaining_pcs,
        },
    )


@login_required
@permission_required("orders.change_order", raise_exception=True)
@transaction.atomic
def production_update(request, pk):
    order = get_object_or_404(
        Order.objects.prefetch_related("items", "designs__items"),
        pk=pk,
        is_deleted=False,
    )

    if request.method == "POST":
        if request.POST.get("cancel_order"):
            cancel_status = _get_cancel_status()

            if order.status == cancel_status:
                messages.warning(request, "Order already cancelled.")
                return redirect("production_detail", pk=order.pk)

            # Cancel = restore stock one time
            if order.stock_deducted:
                restore_stock_for_order(order)

            order.status = cancel_status
            order.save(update_fields=["status"])

            messages.success(request, "Order cancelled and stock restored.")
            return redirect("production_detail", pk=order.pk)

        if request.POST.get("complete_all"):
            for item in order.items.all():
                remaining = Decimal(item.quantity or 0) - Decimal(item.done_qty or 0)

                if remaining > 0:
                    OrderProgress.objects.create(
                        order=order,
                        order_item=item,
                        qty_done=remaining,
                        remark="Auto complete",
                    )

                    item.done_qty = item.quantity
                    item.save(update_fields=["done_qty"])

            order.done_pcs = order.total_pcs
            order.status = Order.STATUS_DONE
            order.save(update_fields=["done_pcs", "status"])

            messages.success(request, "Order marked as COMPLETED.")
            return redirect("production_detail", pk=order.pk)

        item_id = request.POST.get("item_id")
        qty_done = _decimal_or_zero(request.POST.get("qty_done"))
        remark = (request.POST.get("remark") or "").strip()

        order_item = get_object_or_404(order.items, pk=item_id)

        if qty_done <= 0:
            messages.error(request, "Qty must be greater than 0.")
            return redirect("production_detail", pk=order.pk)

        if Decimal(order_item.done_qty or 0) + qty_done > Decimal(order_item.quantity or 0):
            messages.error(request, "Done qty cannot be greater than ordered qty.")
            return redirect("production_detail", pk=order.pk)

        OrderProgress.objects.create(
            order=order,
            order_item=order_item,
            qty_done=qty_done,
            remark=remark,
        )

        order_item.done_qty = Decimal(order_item.done_qty or 0) + qty_done
        order_item.save(update_fields=["done_qty"])

        total_done = sum(Decimal(i.done_qty or 0) for i in order.items.all())
        order.done_pcs = total_done

        if order.done_pcs >= order.total_pcs and order.total_pcs > 0:
            order.done_pcs = order.total_pcs
            order.status = Order.STATUS_DONE
        elif order.done_pcs > 0:
            order.status = Order.STATUS_PROCESSING
        else:
            order.status = Order.STATUS_PENDING

        order.save(update_fields=["done_pcs", "status"])
        messages.success(request, "Production progress updated.")

    return redirect("production_detail", pk=order.pk)

@login_required
@permission_required("orders.view_order", raise_exception=True)
def order_invoice(request, pk):
    order = get_object_or_404(_get_prefetched_order_queryset(), pk=pk)

    return render(
        request,
        "orders/order_invoice.html",
        {
            "order": order,
            "printed_by": request.user,
        },
    )

@login_required
@permission_required("orders.view_order", raise_exception=True)
def order_invoice_pdf(request, pk):
    order = get_object_or_404(_get_prefetched_order_queryset(), pk=pk)

    html = render_to_string(
        "orders/order_invoice.html",
        {
            "order": order,
            "print_mode": True,
            "printed_by": request.user,
        },
        request=request,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(
            viewport={
                "width": 794,
                "height": 1123,
            }
        )

        page.set_content(html, wait_until="networkidle")
        page.emulate_media(media="print")

        pdf = page.pdf(
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            margin={
                "top": "0",
                "right": "0",
                "bottom": "0",
                "left": "0",
            },
        )

        browser.close()

    customer = _safe_download_name(order.customer_name, "customer")
    order_no = _safe_download_name(order.order_no, "invoice")
    filename = f"{customer}_{order_no}.pdf"

    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
@login_required
@permission_required("orders.change_order", raise_exception=True)
def order_edit(request, pk):
    order = get_object_or_404(_get_prefetched_order_queryset(), pk=pk, is_deleted=False)

    if request.method == "POST":
        before_order = _snapshot_order(order)
        form = OrderForm(request.POST, request.FILES, instance=order)

        if form.is_valid():
            try:
                with transaction.atomic():
                    order = form.save(commit=False)
                    order.customer = _get_or_create_customer_from_request(request)
                    order.save()

                    design_payloads = _build_design_payloads_from_post(request)

                    total_amount, total_pcs = _save_design_payloads(
                        order=order,
                        design_payloads=design_payloads,
                        user=request.user,
                        is_edit=True,
                    )

                    discount_amount = _decimal_or_zero(request.POST.get("discount_amount"))
                    shipping_fee = _decimal_or_zero(request.POST.get("shipping_fee"))
                    deposit_amount = _decimal_or_zero(request.POST.get("deposit_amount"))
                    paid_amount = _decimal_or_zero(request.POST.get("paid_amount"))

                    order.total_amount = total_amount - discount_amount + shipping_fee
                    order.deposit_amount = deposit_amount
                    order.paid_amount = paid_amount
                    order.total_pcs = total_pcs

                    if Decimal(order.done_pcs or 0) > total_pcs:
                        order.done_pcs = total_pcs

                    if order.done_pcs >= order.total_pcs and order.total_pcs > 0:
                        order.status = Order.STATUS_DONE
                    elif order.done_pcs > 0:
                        order.status = Order.STATUS_PROCESSING
                    else:
                        order.status = Order.STATUS_PENDING

                    order.save(update_fields=[
                        "customer",
                        "order_type",
                        "service_type",
                        "customer_name",
                        "phone",
                        "customer_location",
                        "deadline",
                        "remark",
                        "total_amount",
                        "deposit_amount",
                        "paid_amount",
                        "total_pcs",
                        "done_pcs",
                        "status",
                    ])

                    after_order = _snapshot_order(order)
                    _log_order_changes(order, before_order, after_order, request.user)

                messages.success(request, f"Order {order.order_no} updated successfully.")
                return redirect("order_detail", pk=order.pk)

            except ValidationError as e:
                messages.error(request, e.messages[0] if hasattr(e, "messages") else str(e))
        else:
            messages.error(request, "Please fix the errors below and try again.")
    else:
        form = OrderForm(instance=order)
        form.fields["discount_amount"].initial = Decimal("0")
        form.fields["shipping_fee"].initial = Decimal("0")

    return render(
        request,
        "orders/order_form.html",
        {
            "form": form,
            "is_edit": True,
            "submit_label": "Update Order",
            "order": order,
            **_order_form_context_base(),
        },
    )


@login_required
@permission_required("orders.delete_order", raise_exception=True)
@transaction.atomic
def order_trash(request, pk):
    order = get_object_or_404(Order, pk=pk, is_deleted=False)

    if request.method == "POST":
        cancel_status = _get_cancel_status()

        # Delete active order = restore stock
        # Delete cancelled order = no restore, because cancel already restored
        if order.status != cancel_status and order.stock_deducted:
            restore_stock_for_order(order)

        order.is_deleted = True
        order.deleted_at = timezone.now()
        order.deleted_by = request.user if request.user.is_authenticated else None
        order.deleted_reason = (request.POST.get("delete_reason") or "").strip()

        order.save(update_fields=[
            "is_deleted",
            "deleted_at",
            "deleted_by",
            "deleted_reason",
        ])

        messages.success(request, f"Order {order.order_no} moved to trash.")
        return redirect("order_trash_list")

    return redirect("order_detail", pk=order.pk)
@login_required
@permission_required("orders.change_order", raise_exception=True)
@transaction.atomic
def order_restore(request, pk):
    order = get_object_or_404(Order, pk=pk, is_deleted=True)

    if request.method == "POST":
        order.is_deleted = False
        order.deleted_at = None
        order.deleted_by = None
        order.deleted_reason = ""

        # Restore = create that order again
        order.status = Order.STATUS_PENDING
        order.done_pcs = Decimal("0")

        order.save(update_fields=[
            "is_deleted",
            "deleted_at",
            "deleted_by",
            "deleted_reason",
            "status",
            "done_pcs",
        ])

        if not order.stock_deducted:
            deduct_stock_for_order(order, allow_shortage=True)

        messages.success(request, f"Order {order.order_no} restored and stock deducted again.")
        return redirect("order_detail", pk=order.pk)

    return redirect("order_trash_list")
@login_required
@permission_required("orders.view_order", raise_exception=True)
def order_trash_list(request):
    qs = (
        Order.objects.filter(is_deleted=True)
        .select_related("deleted_by")
        .prefetch_related("items")
        .order_by("-deleted_at", "-id")
    )

    rows = []

    for order in qs:
        cloth_qty, film_meter = _get_order_totals_by_service(order)

        rows.append({
            "obj": order,
            "cloth_qty": cloth_qty,
            "film_meter": film_meter,
            "reason": order.deleted_reason or "-",
            "deleted_by": order.deleted_by.username if order.deleted_by else "-",
            "deleted_at": order.deleted_at,
        })

    return render(
        request,
        "orders/order_trash_list.html",
        {
            "rows": rows,
            "total_orders": qs.count(),
        },
    )

@login_required
@permission_required("orders.view_order", raise_exception=True)
def order_invoice_png(request, pk):
    order = get_object_or_404(_get_prefetched_order_queryset(), pk=pk)

    html = render_to_string(
        "orders/order_invoice.html",
        {
            "order": order,
            "print_mode": True,
            "printed_by": request.user,
        },
        request=request,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])

        page = browser.new_page(
            viewport={
                "width": 794,
                "height": 1123,
            },
            device_scale_factor=2,
        )

        page.set_content(html, wait_until="networkidle")
        page.emulate_media(media="screen")

        png = page.screenshot(
            type="png",
            full_page=False,
            clip={
                "x": 0,
                "y": 0,
                "width": 794,
                "height": 1123,
            },
        )

        browser.close()

    customer = _safe_download_name(order.customer_name, "customer")
    order_no = _safe_download_name(order.order_no, "invoice")
    filename = f"{customer}_{order_no}.png"

    response = HttpResponse(png, content_type="image/png")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response