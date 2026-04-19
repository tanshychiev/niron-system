from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import OrderForm, OrderItemFormSet, ProductionFilterForm
from .models import Order, OrderDesignFile, OrderHistory, OrderProgress
from .services import deduct_stock_for_order


def _stringify(value):
    if value is None:
        return ""
    return str(value)


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
        "item_mode": item.item_mode,
        "description": item.description,
        "shirt_item": str(item.shirt_item) if item.shirt_item else "",
        "film_item": str(item.film_item) if item.film_item else "",
        "color": str(item.color) if item.color else "",
        "size": str(item.size) if item.size else "",
        "quantity": str(item.quantity or 0),
        "unit_price": str(item.unit_price or 0),
        "manual_film_meter": str(item.manual_film_meter or 0),
        "line_total": str(item.line_total or 0),
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


@login_required
@permission_required("orders.view_order", raise_exception=True)
def order_list(request):
    orders = Order.objects.all().order_by("-created_at", "-id")
    return render(request, "orders/order_list.html", {"orders": orders})


@login_required
@permission_required("orders.add_order", raise_exception=True)
@transaction.atomic
def order_create(request):
    if request.method == "POST":
        form = OrderForm(request.POST, request.FILES)
        formset = OrderItemFormSet(request.POST, request.FILES)

        if form.is_valid() and formset.is_valid():
            try:
                order = form.save(commit=False)
                order.status = Order.STATUS_PENDING
                order.save()

                _log_order_history(
                    order=order,
                    action=OrderHistory.ACTION_CREATE,
                    field_name="order",
                    old_value="",
                    new_value=order.order_no,
                    user=request.user,
                    remark="Order created",
                )

                formset.instance = order
                items = formset.save(commit=False)

                total_amount = Decimal("0")
                total_pcs = Decimal("0")

                for obj in formset.deleted_objects:
                    obj.delete()

                for item in items:
                    if not item.description and not item.shirt_item and not item.film_item:
                        continue

                    item.order = order
                    item.save()

                    total_amount += Decimal(item.line_total or 0)
                    total_pcs += Decimal(item.quantity or 0)

                    _log_order_history(
                        order=order,
                        action=OrderHistory.ACTION_ITEM_ADD,
                        field_name="item",
                        old_value="",
                        new_value=_snapshot_item(item),
                        user=request.user,
                        remark="Item added during create",
                    )

                uploaded_files = request.FILES.getlist("design_files")
                for f in uploaded_files:
                    OrderDesignFile.objects.create(order=order, image=f)
                    _log_order_history(
                        order=order,
                        action=OrderHistory.ACTION_DESIGN_ADD,
                        field_name="design_file",
                        old_value="",
                        new_value=f.name,
                        user=request.user,
                        remark="Design file uploaded",
                    )

                discount_amount = Decimal(request.POST.get("discount_amount") or 0)
                shipping_fee = Decimal(request.POST.get("shipping_fee") or 0)
                deposit_amount = Decimal(request.POST.get("deposit_amount") or 0)
                paid_amount = Decimal(request.POST.get("paid_amount") or 0)

                order.total_amount = total_amount - discount_amount + shipping_fee
                order.deposit_amount = deposit_amount
                order.paid_amount = paid_amount
                order.total_pcs = total_pcs
                order.done_pcs = Decimal("0")
                order.status = Order.STATUS_PENDING
                order.save(
                    update_fields=[
                        "total_amount",
                        "deposit_amount",
                        "paid_amount",
                        "total_pcs",
                        "done_pcs",
                        "status",
                    ]
                )

                deduct_stock_for_order(order)

                messages.success(request, f"Order {order.order_no} created successfully.")
                return redirect("order_detail", pk=order.pk)

            except ValidationError as e:
                messages.error(request, str(e))
        else:
            messages.error(request, "Please fix the errors below and try again.")
    else:
        form = OrderForm()
        formset = OrderItemFormSet()

    return render(
        request,
        "orders/order_form.html",
        {
            "form": form,
            "formset": formset,
            "is_edit": False,
            "submit_label": "Save Order",
        },
    )


@login_required
@permission_required("orders.view_order", raise_exception=True)
def order_detail(request, pk):
    order = get_object_or_404(
        Order.objects.prefetch_related(
            "items",
            "design_files",
            "stock_consumptions__batch_item__item",
        ),
        pk=pk,
    )
    return render(request, "orders/order_detail.html", {"order": order})


@login_required
@permission_required("orders.view_order", raise_exception=True)
def production_list(request):
    form = ProductionFilterForm(request.GET or None)
    qs = Order.objects.all()

    if form.is_valid():
        keyword = (form.cleaned_data.get("q") or "").strip()
        status = form.cleaned_data.get("status") or ProductionFilterForm.STATUS_ACTIVE
        deadline = form.cleaned_data.get("deadline")

        if keyword:
            qs = qs.filter(
                Q(customer_name__icontains=keyword) |
                Q(order_no__icontains=keyword)
            )

        cancel_status = _get_cancel_status()

        if status == ProductionFilterForm.STATUS_ACTIVE:
            qs = qs.filter(
                status__in=[
                    Order.STATUS_PENDING,
                    Order.STATUS_PROCESSING,
                ]
            )
        elif status == ProductionFilterForm.STATUS_DONE:
            qs = qs.filter(status=Order.STATUS_DONE)
        elif status == ProductionFilterForm.STATUS_CANCEL:
            qs = qs.filter(status=cancel_status)

        if deadline:
            qs = qs.filter(deadline__date=deadline)

    qs = qs.order_by("deadline", "-id")

    paginator = Paginator(qs, 50)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    start_no = (page_obj.number - 1) * paginator.per_page
    now = timezone.now()

    rows = []
    for idx, order in enumerate(page_obj.object_list, start=1):
        deadline = getattr(order, "deadline", None)

        if deadline:
            if now > deadline:
                countdown_text = "Overdue"
            else:
                diff = deadline - now
                total_seconds = int(diff.total_seconds())
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                countdown_text = f"{hours}h {minutes}m"
        else:
            countdown_text = "-"

        rows.append(
            {
                "no": start_no + idx,
                "order": order,
                "countdown_text": countdown_text,
                "status_color": _status_badge(order.status),
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
            "now": now,
        },
    )


@login_required
@permission_required("orders.view_order", raise_exception=True)
def production_detail(request, pk):
    order = get_object_or_404(
        Order.objects.prefetch_related(
            "items",
            "design_files",
            "progress_logs",
        ),
        pk=pk,
    )

    remaining_pcs = Decimal(order.total_pcs or 0) - Decimal(order.done_pcs or 0)
    if remaining_pcs < 0:
        remaining_pcs = Decimal("0")

    return render(
        request,
        "orders/production_detail.html",
        {
            "order": order,
            "remaining_pcs": remaining_pcs,
        },
    )


@login_required
@permission_required("orders.change_order", raise_exception=True)
@transaction.atomic
def production_update(request, pk):
    order = get_object_or_404(Order, pk=pk)

    if request.method == "POST":
        if request.POST.get("cancel_order"):
            cancel_status = _get_cancel_status()
            order.status = cancel_status
            order.save(update_fields=["status"])
            messages.success(request, "Order marked as CANCEL.")
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
        qty_done = Decimal(request.POST.get("qty_done") or 0)
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

        if order.done_pcs >= order.total_pcs:
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
    order = get_object_or_404(
        Order.objects.prefetch_related(
            "items",
            "design_files",
        ),
        pk=pk,
    )
    return render(request, "orders/order_invoice.html", {"order": order})


@login_required
@permission_required("orders.view_order", raise_exception=True)
def order_invoice_pdf(request, pk):
    order = get_object_or_404(
        Order.objects.prefetch_related(
            "items",
            "design_files",
        ),
        pk=pk,
    )
    return render(
        request,
        "orders/order_invoice_pdf.html",
        {
            "order": order,
            "print_mode": True,
        },
    )


@login_required
@permission_required("orders.change_order", raise_exception=True)
@transaction.atomic
def order_edit(request, pk):
    order = get_object_or_404(
        Order.objects.prefetch_related("items", "design_files", "history_logs"),
        pk=pk,
    )

    if request.method == "POST":
        before_order = _snapshot_order(order)
        existing_items = {item.pk: _snapshot_item(item) for item in order.items.all()}

        form = OrderForm(request.POST, request.FILES, instance=order)
        formset = OrderItemFormSet(request.POST, request.FILES, instance=order)

        if form.is_valid() and formset.is_valid():
            try:
                order = form.save(commit=False)
                order.save()

                items = formset.save(commit=False)

                total_amount = Decimal("0")
                total_pcs = Decimal("0")

                for obj in formset.deleted_objects:
                    old_item = existing_items.get(obj.pk, {})
                    _log_order_history(
                        order=order,
                        action=OrderHistory.ACTION_ITEM_DELETE,
                        field_name=f"item#{obj.pk}",
                        old_value=old_item,
                        new_value="",
                        user=request.user,
                        remark="Item removed",
                    )
                    obj.delete()

                for item in items:
                    if not item.description and not item.shirt_item and not item.film_item:
                        continue

                    is_new = item.pk is None
                    old_item_data = existing_items.get(item.pk, {}) if item.pk else {}

                    item.order = order
                    item.save()

                    total_amount += Decimal(item.line_total or 0)
                    total_pcs += Decimal(item.quantity or 0)

                    if is_new:
                        _log_order_history(
                            order=order,
                            action=OrderHistory.ACTION_ITEM_ADD,
                            field_name="item",
                            old_value="",
                            new_value=_snapshot_item(item),
                            user=request.user,
                            remark="New item added",
                        )
                    else:
                        new_item_data = _snapshot_item(item)
                        for key, old_val in old_item_data.items():
                            new_val = new_item_data.get(key)
                            if _stringify(old_val) != _stringify(new_val):
                                _log_order_history(
                                    order=order,
                                    action=OrderHistory.ACTION_ITEM_EDIT,
                                    field_name=f"item#{item.pk}.{key}",
                                    old_value=old_val,
                                    new_value=new_val,
                                    user=request.user,
                                    remark="Item updated",
                                )

                uploaded_files = request.FILES.getlist("design_files")
                for f in uploaded_files:
                    OrderDesignFile.objects.create(order=order, image=f)
                    _log_order_history(
                        order=order,
                        action=OrderHistory.ACTION_DESIGN_ADD,
                        field_name="design_file",
                        old_value="",
                        new_value=f.name,
                        user=request.user,
                        remark="Design file uploaded on edit",
                    )

                discount_amount = Decimal(request.POST.get("discount_amount") or 0)
                shipping_fee = Decimal(request.POST.get("shipping_fee") or 0)
                deposit_amount = Decimal(request.POST.get("deposit_amount") or 0)
                paid_amount = Decimal(request.POST.get("paid_amount") or 0)

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

                order.save(
                    update_fields=[
                        "order_type",
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
                    ]
                )

                after_order = _snapshot_order(order)
                _log_order_changes(order, before_order, after_order, request.user)

                messages.success(request, f"Order {order.order_no} updated successfully.")
                return redirect("order_detail", pk=order.pk)

            except ValidationError as e:
                messages.error(request, str(e))
        else:
            messages.error(request, "Please fix the errors below and try again.")
    else:
        form = OrderForm(instance=order)
        form.fields["discount_amount"].initial = Decimal("0")
        form.fields["shipping_fee"].initial = Decimal("0")
        formset = OrderItemFormSet(instance=order)

    return render(
        request,
        "orders/order_form.html",
        {
            "form": form,
            "formset": formset,
            "is_edit": True,
            "submit_label": "Update Order",
            "order": order,
        },
    )