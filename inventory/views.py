from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from orders.models import Order, OrderItem, StockConsumption

from .forms import (
    ColorForm,
    InventoryAdjustmentForm,
    InventoryAdjustStockSelectForm,
    InventoryAdjustVariantForm,
    InventoryBatchForm,
    InventoryBatchItemFormSet,
    InventoryItemForm,
    SizeForm,
)
from .models import (
    Color,
    InventoryAdjustment,
    InventoryBatch,
    InventoryBatchHistory,
    InventoryBatchItem,
    InventoryItem,
    Size,
)


def _batch_snapshot(batch):
    return {
        "batch_no": batch.batch_no,
        "supplier": batch.supplier,
        "received_date": str(batch.received_date),
        "status": batch.status,
        "note": batch.note,
        "total_goods_cost": str(batch.total_goods_cost or 0),
        "shipping_cost": str(batch.shipping_cost or 0),
        "extra_cost": str(batch.extra_cost or 0),
        "rows": [
            {
                "id": row.id,
                "item_code": row.item.code if row.item else "",
                "item_name": row.item.name if row.item else "",
                "color": row.color.name if row.color else "",
                "size": row.size.name if row.size else "",
                "qty_received": str(row.qty_received),
                "qty_remaining": str(row.qty_remaining),
            }
            for row in batch.items.select_related("item", "color", "size").all()
        ],
    }


def _log_batch_history(batch, action, user=None, note=""):
    InventoryBatchHistory.objects.create(
        batch=batch,
        action=action,
        changed_by=user if user and user.is_authenticated else None,
        note=note,
        snapshot_json=_batch_snapshot(batch),
    )


def _ensure_size_row(grouped, key, size_obj):
    size_name = size_obj.name if size_obj else "-"
    size_sort = size_obj.sort_order if size_obj else 9999

    if size_name not in grouped[key]["sizes"]:
        grouped[key]["sizes"][size_name] = {
            "size_name": size_name,
            "size_sort": size_sort,
            "stock_qty": 0,
            "reserved_qty": 0,
            "available_qty": 0,
            "in_progress_qty": 0,
            "total_qty": 0,
            "shortage_qty": 0,
        }

    return grouped[key]["sizes"][size_name]


@login_required
@permission_required("inventory.view_inventorybatch", raise_exception=True)
def inventory_list(request):
    items = InventoryItem.objects.all().order_by("code", "name")
    batches = InventoryBatch.objects.filter(is_deleted=False).order_by("-received_date", "-id")

    grouped = defaultdict(
        lambda: {
            "item_id": None,
            "item_code": "",
            "item_name": "",
            "item_style": InventoryItem.STYLE_OVERSIZE,
            "item_style_label": "Oversize",
            "color_id": None,
            "color_name": "-",
            "color_hex": "#D1D5DB",

            # stock_qty = physical stock before active orders reserve it
            # reserved_qty = qty already deducted by active orders
            # in_progress_qty = active order remaining qty
            # available_qty = stock_qty - in_progress_qty
            # total_qty = stock_qty
            "stock_qty": 0,
            "reserved_qty": 0,
            "available_qty": 0,
            "in_progress_qty": 0,
            "total_qty": 0,
            "shortage_qty": 0,
            "sizes": {},
        }
    )

    active_statuses = [
        Order.STATUS_PENDING,
        Order.STATUS_PROCESSING,
    ]

    # 1) Current remaining stock in batch rows.
    stock_rows = (
        InventoryBatchItem.objects.select_related("item", "color", "size", "batch")
        .filter(
            is_active=True,
            item__item_type=InventoryItem.TYPE_SHIRT,
            batch__is_deleted=False,
        )
        .order_by(
            "item__sample_style",
            "item__code",
            "item__name",
            "color__name",
            "size__sort_order",
            "size__id",
            "id",
        )
    )

    for row in stock_rows:
        key = (row.item_id, row.color_id or 0)

        grouped[key]["item_id"] = row.item_id
        grouped[key]["item_code"] = row.item.code
        grouped[key]["item_name"] = row.item.name
        grouped[key]["item_style"] = getattr(row.item, "sample_style", InventoryItem.STYLE_OVERSIZE)
        grouped[key]["item_style_label"] = row.item.get_sample_style_display()
        grouped[key]["color_id"] = row.color_id
        grouped[key]["color_name"] = row.color.name if row.color else "-"
        grouped[key]["color_hex"] = getattr(row.color, "hex_code", "#D1D5DB") if row.color else "#D1D5DB"

        qty = float(row.qty_remaining or 0)
        grouped[key]["stock_qty"] += qty

        size_row = _ensure_size_row(grouped, key, row.size)
        size_row["stock_qty"] += qty

    # 2) Stock already deducted by active orders.
    # This adds reserved stock back into "total physical stock".
    reserved_rows = (
        StockConsumption.objects.select_related(
            "order",
            "batch_item",
            "batch_item__item",
            "batch_item__color",
            "batch_item__size",
            "batch_item__batch",
        )
        .filter(
            order__status__in=active_statuses,
            order__is_deleted=False,
            batch_item__item__item_type=InventoryItem.TYPE_SHIRT,
            batch_item__batch__is_deleted=False,
        )
    )

    for c in reserved_rows:
        batch_item = c.batch_item
        item = batch_item.item
        color = batch_item.color
        size = batch_item.size

        key = (item.id, color.id if color else 0)

        grouped[key]["item_id"] = item.id
        grouped[key]["item_code"] = item.code
        grouped[key]["item_name"] = item.name
        grouped[key]["item_style"] = getattr(item, "sample_style", InventoryItem.STYLE_OVERSIZE)
        grouped[key]["item_style_label"] = item.get_sample_style_display()
        grouped[key]["color_id"] = color.id if color else None
        grouped[key]["color_name"] = color.name if color else "-"
        grouped[key]["color_hex"] = getattr(color, "hex_code", "#D1D5DB") if color else "#D1D5DB"

        qty = float(c.consumed_qty or 0)
        grouped[key]["reserved_qty"] += qty

        size_row = _ensure_size_row(grouped, key, size)
        size_row["reserved_qty"] += qty

    # 3) Active order demand.
    progress_rows = (
        OrderItem.objects.select_related("shirt_item", "color", "size", "order")
        .filter(
            shirt_item__isnull=False,
            order__status__in=active_statuses,
            order__is_deleted=False,
        )
    )

    for row in progress_rows:
        key = (row.shirt_item_id, row.color_id or 0)

        grouped[key]["item_id"] = row.shirt_item_id
        grouped[key]["item_code"] = row.shirt_item.code
        grouped[key]["item_name"] = row.shirt_item.name
        grouped[key]["item_style"] = getattr(row.shirt_item, "sample_style", InventoryItem.STYLE_OVERSIZE)
        grouped[key]["item_style_label"] = row.shirt_item.get_sample_style_display()
        grouped[key]["color_id"] = row.color_id
        grouped[key]["color_name"] = row.color.name if row.color else "-"
        grouped[key]["color_hex"] = getattr(row.color, "hex_code", "#D1D5DB") if row.color else "#D1D5DB"

        remaining_qty = Decimal(row.quantity or 0) - Decimal(row.done_qty or 0)
        if remaining_qty < 0:
            remaining_qty = Decimal("0")

        qty = float(remaining_qty)
        grouped[key]["in_progress_qty"] += qty

        size_row = _ensure_size_row(grouped, key, row.size)
        size_row["in_progress_qty"] += qty

    variant_cards = []

    for _, data in grouped.items():
        stock_qty = float(data.get("stock_qty", 0)) + float(data.get("reserved_qty", 0))
        in_progress_qty = float(data.get("in_progress_qty", 0))

        data["total_qty"] = stock_qty
        data["available_qty"] = stock_qty - in_progress_qty
        data["shortage_qty"] = max(in_progress_qty - stock_qty, 0)

        size_list = []
        for _, s in sorted(
            data["sizes"].items(),
            key=lambda x: (x[1]["size_sort"], x[1]["size_name"]),
        ):
            size_stock_qty = float(s.get("stock_qty", 0)) + float(s.get("reserved_qty", 0))
            size_in_progress_qty = float(s.get("in_progress_qty", 0))

            s["total_qty"] = size_stock_qty
            s["available_qty"] = size_stock_qty - size_in_progress_qty
            s["shortage_qty"] = max(size_in_progress_qty - size_stock_qty, 0)

            size_list.append(s)

        data["sizes"] = size_list
        variant_cards.append(data)

    style_order = {
        InventoryItem.STYLE_OVERSIZE: 1,
        InventoryItem.STYLE_POLO: 2,
        InventoryItem.STYLE_BOXY: 3,
    }

    grouped_styles = defaultdict(list)
    for card in variant_cards:
        grouped_styles[card["item_style"]].append(card)

    style_groups = []
    for style_key, cards in grouped_styles.items():
        style_groups.append(
            {
                "style_key": style_key,
                "style_label": cards[0]["item_style_label"],
                "cards": cards,
                "sort_order": style_order.get(style_key, 999),
            }
        )

    style_groups = sorted(style_groups, key=lambda x: x["sort_order"])

    batch_rows = []
    for batch in batches:
        total_cloth = 0
        for item in batch.items.all():
            if item.item and item.item.item_type == InventoryItem.TYPE_SHIRT:
                total_cloth += float(item.qty_received or 0)

        batch_rows.append(
            {
                "id": batch.id,
                "batch_no": batch.batch_no,
                "supplier": batch.supplier or "-",
                "created_by": batch.created_by.username if batch.created_by else "-",
                "received_date": batch.received_date,
                "total_cloth": total_cloth,
            }
        )

    return render(
        request,
        "inventory/inventory_list.html",
        {
            "items": items,
            "style_groups": style_groups,
            "batches": batch_rows,
        },
    )


@login_required
@permission_required("inventory.view_inventoryitem", raise_exception=True)
def inventory_item_list(request):
    items = InventoryItem.objects.all().order_by("code", "name")
    return render(request, "inventory/inventory_item_list.html", {"items": items})


@login_required
@permission_required("inventory.add_inventoryitem", raise_exception=True)
def inventory_item_create(request):
    if request.method == "POST":
        form = InventoryItemForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Inventory item created.")
            return redirect("inventory_item_list")
    else:
        form = InventoryItemForm()

    return render(
        request,
        "inventory/inventory_item_form.html",
        {
            "form": form,
            "page_title": "Create Item",
            "submit_label": "Save Item",
        },
    )


@login_required
@permission_required("inventory.change_inventoryitem", raise_exception=True)
def inventory_item_edit(request, pk):
    item = get_object_or_404(InventoryItem, pk=pk)

    if request.method == "POST":
        form = InventoryItemForm(request.POST, instance=item)
        if form.is_valid():
            form.save()
            messages.success(request, "Item updated successfully.")
            return redirect("inventory_item_list")
    else:
        form = InventoryItemForm(instance=item)

    return render(
        request,
        "inventory/inventory_item_form.html",
        {
            "form": form,
            "page_title": "Edit Item",
            "submit_label": "Update Item",
        },
    )


@login_required
@permission_required("inventory.view_color", raise_exception=True)
def color_list(request):
    colors = Color.objects.all().order_by("name")
    return render(request, "inventory/color_list.html", {"colors": colors})


@login_required
@permission_required("inventory.add_color", raise_exception=True)
def color_create(request):
    if request.method == "POST":
        form = ColorForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Color created.")
            return redirect("color_list")
    else:
        form = ColorForm()

    return render(
        request,
        "inventory/color_form.html",
        {
            "form": form,
            "page_title": "Create Color",
            "submit_label": "Save Color",
        },
    )


@login_required
@permission_required("inventory.change_color", raise_exception=True)
def color_edit(request, pk):
    color = get_object_or_404(Color, pk=pk)

    if request.method == "POST":
        form = ColorForm(request.POST, instance=color)
        if form.is_valid():
            form.save()
            messages.success(request, "Color updated successfully.")
            return redirect("color_list")
    else:
        form = ColorForm(instance=color)

    return render(
        request,
        "inventory/color_form.html",
        {
            "form": form,
            "page_title": "Edit Color",
            "submit_label": "Update Color",
        },
    )


@login_required
@permission_required("inventory.view_size", raise_exception=True)
def size_list(request):
    sizes = Size.objects.all().order_by("sort_order", "id")
    return render(request, "inventory/size_list.html", {"sizes": sizes})


@login_required
@permission_required("inventory.add_size", raise_exception=True)
def size_create(request):
    if request.method == "POST":
        form = SizeForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Size created.")
            return redirect("size_list")
    else:
        form = SizeForm()

    return render(
        request,
        "inventory/size_form.html",
        {
            "form": form,
            "page_title": "Create Size",
            "submit_label": "Save Size",
        },
    )


@login_required
@permission_required("inventory.change_size", raise_exception=True)
def size_edit(request, pk):
    size = get_object_or_404(Size, pk=pk)

    if request.method == "POST":
        form = SizeForm(request.POST, instance=size)
        if form.is_valid():
            form.save()
            messages.success(request, "Size updated successfully.")
            return redirect("size_list")
    else:
        form = SizeForm(instance=size)

    return render(
        request,
        "inventory/size_form.html",
        {
            "form": form,
            "page_title": "Edit Size",
            "submit_label": "Update Size",
        },
    )


@login_required
@permission_required("inventory.add_inventorybatch", raise_exception=True)
@transaction.atomic
def inventory_batch_create(request):
    if request.method == "POST":
        form = InventoryBatchForm(request.POST)
        formset = InventoryBatchItemFormSet(request.POST)

        if form.is_valid() and formset.is_valid():
            batch = form.save(commit=False)
            if request.user.is_authenticated:
                batch.created_by = request.user
                batch.updated_by = request.user
            batch.save()

            formset.instance = batch
            items = formset.save(commit=False)

            for obj in formset.deleted_objects:
                obj.delete()

            for item in items:
                if not item.item:
                    continue
                item.batch = batch
                item.base_unit_cost = 0
                item.final_unit_cost = 0
                item.is_active = True
                item.save()

            _log_batch_history(batch, InventoryBatchHistory.ACTION_CREATE, request.user, "Batch created")
            messages.success(request, f"Inventory batch {batch.batch_no} created.")
            return redirect("inventory_batch_detail", pk=batch.pk)
    else:
        form = InventoryBatchForm(initial={"received_date": timezone.localdate()})
        formset = InventoryBatchItemFormSet()

    return render(
        request,
        "inventory/inventory_batch_form.html",
        {
            "form": form,
            "formset": formset,
            "page_title": "Stock In",
            "submit_label": "Save Batch",
        },
    )


@login_required
@permission_required("inventory.change_inventorybatch", raise_exception=True)
@transaction.atomic
def inventory_batch_edit(request, pk):
    batch = get_object_or_404(InventoryBatch, pk=pk, is_deleted=False)

    if request.method == "POST":
        form = InventoryBatchForm(request.POST, instance=batch)
        formset = InventoryBatchItemFormSet(request.POST, instance=batch)

        if form.is_valid() and formset.is_valid():
            batch = form.save(commit=False)
            if request.user.is_authenticated:
                batch.updated_by = request.user
            batch.save()

            items = formset.save(commit=False)

            for obj in formset.deleted_objects:
                if obj.qty_used != 0:
                    messages.error(request, "Cannot delete a row that already has stock used.")
                    return redirect("inventory_batch_edit", pk=batch.pk)
                obj.delete()

            for item in items:
                if not item.item:
                    continue
                item.batch = batch
                item.base_unit_cost = 0
                item.final_unit_cost = 0
                item.is_active = True
                item.save()

            _log_batch_history(batch, InventoryBatchHistory.ACTION_UPDATE, request.user, "Batch updated")
            messages.success(request, f"Batch {batch.batch_no} updated.")
            return redirect("inventory_batch_detail", pk=batch.pk)
    else:
        form = InventoryBatchForm(instance=batch)
        formset = InventoryBatchItemFormSet(instance=batch)

    return render(
        request,
        "inventory/inventory_batch_form.html",
        {
            "form": form,
            "formset": formset,
            "batch": batch,
            "page_title": f"Edit Batch {batch.batch_no}",
            "submit_label": "Update Batch",
        },
    )


@login_required
@permission_required("inventory.delete_inventorybatch", raise_exception=True)
@transaction.atomic
def inventory_batch_delete(request, pk):
    batch = get_object_or_404(InventoryBatch, pk=pk, is_deleted=False)

    if request.method == "POST":
        batch.is_deleted = True
        batch.deleted_at = timezone.now()
        if request.user.is_authenticated:
            batch.deleted_by = request.user
            batch.updated_by = request.user
        batch.save(update_fields=["is_deleted", "deleted_at", "deleted_by", "updated_by", "updated_at"])

        _log_batch_history(batch, InventoryBatchHistory.ACTION_DELETE, request.user, "Batch soft deleted")
        messages.success(request, f"Batch {batch.batch_no} deleted.")
        return redirect("inventory_list")

    return render(request, "inventory/inventory_batch_delete.html", {"batch": batch})


@login_required
@permission_required("inventory.view_inventorybatch", raise_exception=True)
def inventory_batch_detail(request, pk):
    batch = get_object_or_404(
        InventoryBatch.objects.prefetch_related(
            "items__item",
            "items__color",
            "items__size",
            "history_logs__changed_by",
        ),
        pk=pk,
    )
    return render(request, "inventory/inventory_batch_detail.html", {"batch": batch})


@login_required
@permission_required("inventory.view_inventorybatchhistory", raise_exception=True)
def inventory_batch_history(request, pk):
    batch = get_object_or_404(
        InventoryBatch.objects.prefetch_related("history_logs__changed_by"),
        pk=pk,
    )
    return render(
        request,
        "inventory/inventory_batch_history.html",
        {
            "batch": batch,
            "history_logs": batch.history_logs.all(),
        },
    )


@login_required
@permission_required("inventory.add_inventoryadjustment", raise_exception=True)
@transaction.atomic
def inventory_adjustment_create(request, batch_item_id):
    batch_item = get_object_or_404(
        InventoryBatchItem.objects.select_related("item", "color", "size", "batch"),
        pk=batch_item_id,
        batch__is_deleted=False,
    )

    if request.method == "POST":
        form = InventoryAdjustmentForm(request.POST, batch_item=batch_item)

        if form.is_valid():
            adjustment = form.save(commit=False)
            adjustment.batch_item = batch_item
            adjustment.created_by = request.user if request.user.is_authenticated else None

            old_qty = batch_item.qty_remaining or Decimal("0")
            adjustment.qty_before = old_qty

            adjustment_type = form.cleaned_data["adjustment_type"]
            qty = form.cleaned_data.get("qty") or Decimal("0")
            stocktake_final_qty = form.cleaned_data.get("stocktake_final_qty")

            if adjustment_type == InventoryAdjustment.TYPE_STOCKTAKE:
                new_qty = stocktake_final_qty
                diff = abs((new_qty or Decimal("0")) - old_qty)
                adjustment.qty = diff
            elif adjustment_type in [InventoryAdjustment.TYPE_ADD, InventoryAdjustment.TYPE_FOUND]:
                new_qty = old_qty + qty
            elif adjustment_type in [
                InventoryAdjustment.TYPE_REMOVE,
                InventoryAdjustment.TYPE_DAMAGE,
                InventoryAdjustment.TYPE_LOST,
            ]:
                new_qty = old_qty - qty
                if new_qty < 0:
                    messages.error(request, "Cannot reduce below 0.")
                    return redirect("inventory_adjustment_create", batch_item_id=batch_item.pk)
            else:
                messages.error(request, "Invalid adjustment type.")
                return redirect("inventory_adjustment_create", batch_item_id=batch_item.pk)

            adjustment.qty_after = new_qty
            adjustment.save()

            batch_item.qty_remaining = new_qty
            batch_item.save(update_fields=["qty_remaining"])

            _log_batch_history(
                batch_item.batch,
                InventoryBatchHistory.ACTION_UPDATE,
                request.user,
                f"Stock adjusted for row {batch_item.id}: {adjustment.adjustment_type}",
            )

            messages.success(request, "Stock adjusted successfully.")
            return redirect("inventory_batch_detail", pk=batch_item.batch_id)
    else:
        form = InventoryAdjustmentForm(batch_item=batch_item)

    return render(
        request,
        "inventory/inventory_adjustment_form.html",
        {
            "form": form,
            "batch_item": batch_item,
        },
    )


@login_required
@permission_required("inventory.view_inventoryadjustment", raise_exception=True)
def inventory_adjustment_list(request):
    adjustments = (
        InventoryAdjustment.objects.select_related(
            "batch_item__batch",
            "batch_item__item",
            "batch_item__color",
            "batch_item__size",
            "created_by",
        )
        .order_by("-created_at", "-id")
    )

    return render(request, "inventory/inventory_adjustment_list.html", {"adjustments": adjustments})


@login_required
@permission_required("inventory.add_inventoryadjustment", raise_exception=True)
@transaction.atomic
def inventory_adjust_stock_select(request):
    select_form = InventoryAdjustStockSelectForm(request.GET or None)
    adjust_form = None
    selected_rows = []
    total_available = Decimal("0")
    selected_item = None
    selected_color = None
    selected_size = None

    if select_form.is_valid():
        selected_item = select_form.cleaned_data.get("item")
        selected_color = select_form.cleaned_data.get("color")
        selected_size = select_form.cleaned_data.get("size")

        qs = InventoryBatchItem.objects.select_related("batch", "item", "color", "size").filter(
            is_active=True,
            batch__is_deleted=False,
            item=selected_item,
            qty_remaining__gt=0,
        )

        if selected_color:
            qs = qs.filter(color=selected_color)
        if selected_size:
            qs = qs.filter(size=selected_size)

        selected_rows = list(qs.order_by("-batch__received_date", "-id"))
        total_available = qs.aggregate(total=Sum("qty_remaining")).get("total") or Decimal("0")

        if request.method == "POST":
            adjust_form = InventoryAdjustVariantForm(request.POST)

            if adjust_form.is_valid():
                adjustment_type = adjust_form.cleaned_data["adjustment_type"]
                qty = adjust_form.cleaned_data.get("qty") or Decimal("0")
                final_qty = adjust_form.cleaned_data.get("final_qty")
                reason = adjust_form.cleaned_data.get("reason") or ""

                if adjustment_type == "STOCKTAKE":
                    diff = final_qty - total_available

                    if diff == 0:
                        messages.success(request, "No stock change needed.")
                        return redirect(request.path + "?" + request.META.get("QUERY_STRING", ""))

                    if diff > 0:
                        target = selected_rows[0] if selected_rows else None
                        if not target:
                            messages.error(request, "No stock row found to add into.")
                            return redirect(request.path + "?" + request.META.get("QUERY_STRING", ""))

                        old_qty = target.qty_remaining
                        target.qty_remaining = old_qty + diff
                        target.save(update_fields=["qty_remaining"])

                        InventoryAdjustment.objects.create(
                            batch_item=target,
                            adjustment_type=InventoryAdjustment.TYPE_FOUND,
                            qty=diff,
                            reason=reason or f"Stock take adjusted total from {total_available} to {final_qty}",
                            created_by=request.user if request.user.is_authenticated else None,
                            qty_before=old_qty,
                            qty_after=target.qty_remaining,
                        )
                    else:
                        remaining_to_reduce = abs(diff)

                        for row in selected_rows:
                            if remaining_to_reduce <= 0:
                                break

                            use_qty = min(row.qty_remaining, remaining_to_reduce)
                            old_qty = row.qty_remaining
                            row.qty_remaining = old_qty - use_qty
                            row.save(update_fields=["qty_remaining"])

                            InventoryAdjustment.objects.create(
                                batch_item=row,
                                adjustment_type=InventoryAdjustment.TYPE_STOCKTAKE,
                                qty=use_qty,
                                reason=reason or f"Stock take adjusted total from {total_available} to {final_qty}",
                                created_by=request.user if request.user.is_authenticated else None,
                                qty_before=old_qty,
                                qty_after=row.qty_remaining,
                            )

                            remaining_to_reduce -= use_qty
                else:
                    if adjustment_type in ["ADD", "FOUND"]:
                        target = selected_rows[0] if selected_rows else None
                        if not target:
                            messages.error(request, "No stock row found to add into.")
                            return redirect(request.path + "?" + request.META.get("QUERY_STRING", ""))

                        old_qty = target.qty_remaining
                        target.qty_remaining = old_qty + qty
                        target.save(update_fields=["qty_remaining"])

                        InventoryAdjustment.objects.create(
                            batch_item=target,
                            adjustment_type=InventoryAdjustment.TYPE_FOUND if adjustment_type == "FOUND" else InventoryAdjustment.TYPE_ADD,
                            qty=qty,
                            reason=reason,
                            created_by=request.user if request.user.is_authenticated else None,
                            qty_before=old_qty,
                            qty_after=target.qty_remaining,
                        )
                    else:
                        remaining_to_reduce = qty

                        if remaining_to_reduce > total_available:
                            messages.error(request, "Cannot reduce more than total available stock.")
                            return redirect(request.path + "?" + request.META.get("QUERY_STRING", ""))

                        type_map = {
                            "REMOVE": InventoryAdjustment.TYPE_REMOVE,
                            "LOST": InventoryAdjustment.TYPE_LOST,
                            "DAMAGE": InventoryAdjustment.TYPE_DAMAGE,
                        }

                        for row in selected_rows:
                            if remaining_to_reduce <= 0:
                                break

                            use_qty = min(row.qty_remaining, remaining_to_reduce)
                            old_qty = row.qty_remaining
                            row.qty_remaining = old_qty - use_qty
                            row.save(update_fields=["qty_remaining"])

                            InventoryAdjustment.objects.create(
                                batch_item=row,
                                adjustment_type=type_map[adjustment_type],
                                qty=use_qty,
                                reason=reason,
                                created_by=request.user if request.user.is_authenticated else None,
                                qty_before=old_qty,
                                qty_after=row.qty_remaining,
                            )

                            remaining_to_reduce -= use_qty

                messages.success(request, "Stock adjusted successfully.")
                return redirect(request.path + "?" + request.META.get("QUERY_STRING", ""))
        else:
            adjust_form = InventoryAdjustVariantForm()
    else:
        adjust_form = InventoryAdjustVariantForm() if request.method == "POST" else None

    return render(
        request,
        "inventory/inventory_adjust_stock_select.html",
        {
            "form": select_form,
            "adjust_form": adjust_form,
            "total_available": total_available,
            "selected_item": selected_item,
            "selected_color": selected_color,
            "selected_size": selected_size,
        },
    )