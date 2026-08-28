import logging
import uuid
from collections import OrderedDict, defaultdict
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.db.models import F, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from orders.models import Order, OrderItem
from production.forms import FabricReceiptHeaderForm, fabric_receipt_line_formset
from production.models import FabricReceipt, FabricRoll, ProductionSupplier
from production.services import create_fabric_rolls
from finance.models import Expense

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
    StockLedger,
)
from .stock_ledger import (
    correct_stock_count,
    log_adjustment,
    log_batch_delete,
    log_batch_edit,
    log_stock_in,
)

logger = logging.getLogger(__name__)


def _can_view_stock_cost(user):
    """Only users with Finance expense view permission see actual cost amounts."""
    return bool(
        user
        and user.is_authenticated
        and (user.is_superuser or user.has_perm("finance.view_expense"))
    )


def _sync_inventory_batch_expense(batch, user):
    """One Finance Expense per InventoryBatch; update it instead of duplicating."""
    expense, _ = Expense.objects.get_or_create(
        expense_type=Expense.TYPE_BATCH,
        batch=batch,
        defaults={
            "created_by": user,
            "batch_created_at": batch.created_at,
            "stock_source_type": Expense.SOURCE_INVENTORY,
            "source_reference": batch.batch_no,
        },
    )
    expense.batch_created_at = batch.created_at
    expense.batch_total_cloth = int(batch.total_cloth or 0)
    expense.batch_cost = batch.total_goods_cost or Decimal("0.00")
    expense.batch_delivery_fee = batch.shipping_cost or Decimal("0.00")
    expense.batch_other_fee = batch.extra_cost or Decimal("0.00")
    expense.amount = expense.batch_cost + expense.batch_delivery_fee + expense.batch_other_fee
    expense.stock_source_type = Expense.SOURCE_INVENTORY
    expense.source_reference = batch.batch_no
    expense.supplier_name = batch.supplier_name
    expense.received_date = batch.received_date if batch.status == InventoryBatch.STATUS_RECEIVED else None
    expense.expense_status = (
        Expense.STATUS_COMPLETED if batch.cost_is_added else Expense.STATUS_PENDING
    )
    expense.note = (
        f"Cost added from Stock In {batch.batch_no}."
        if batch.cost_is_added
        else f"Auto-created from Stock In {batch.batch_no}. Cost pending."
    )
    expense.save()
    return expense


def _apply_batch_cost_from_post(batch, request):
    """Save a cost entered by staff/admin. The template controls later visibility."""
    batch.total_goods_cost = Decimal(request.POST.get("total_goods_cost") or "0")
    batch.shipping_cost = Decimal(request.POST.get("shipping_cost") or "0")
    batch.extra_cost = Decimal(request.POST.get("extra_cost") or "0")
    batch.cost_is_added = True
    batch.cost_added_at = timezone.now()
    batch.cost_added_by = request.user



def _to_int(value):
    if value is None:
        return 0
    return int(round(float(value)))


# Fixed display order for shirt sizes.
# The database sort_order is only used for sizes not listed here.
SHIRT_SIZE_ORDER = {
    "XS": 1,
    "S": 2,
    "M": 3,
    "L": 4,
    "XL": 5,
    "XXL": 6,
    "XXXL": 7,
}


# These important colors must appear before all other colors.
PRIORITY_COLOR_ORDER = {
    "black": 1,
    "white": 2,
    "cream": 3,
    "grey": 4,
    "gray": 4,
}


def _shirt_size_sort_key(size_data):
    size_name = str(size_data.get("size_name") or "").strip().upper()
    database_order = int(size_data.get("size_sort") or 9999)

    return (
        SHIRT_SIZE_ORDER.get(size_name, 1000 + database_order),
        size_name,
    )


def _cloth_card_sort_key(card):
    color_name = str(card.get("color_name") or "").strip().lower()

    return (
        PRIORITY_COLOR_ORDER.get(color_name, 999),
        color_name,
        str(card.get("item_code") or "").lower(),
        str(card.get("item_name") or "").lower(),
    )


def _batch_snapshot(batch):
    return {
        "batch_no": batch.batch_no,
        "supplier": batch.supplier_name,
        "supplier_id": batch.supplier_ref_id,
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
                "is_active": row.is_active,
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


@login_required
@permission_required("inventory.view_inventorybatch", raise_exception=True)
def inventory_list(request):
    inventory_type = (request.GET.get("type") or "all").strip().lower()
    if inventory_type not in {"all", "cloth", "printing", "fabric"}:
        inventory_type = "all"

    q = (request.GET.get("q") or "").strip()
    items = InventoryItem.objects.filter(is_active=True).order_by("code", "name")
    batches = InventoryBatch.objects.filter(is_deleted=False).order_by("-received_date", "-id")

    active_statuses = [
        Order.STATUS_PENDING,
        Order.STATUS_PROCESSING,
    ]

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
            "stock_qty": 0,
            "available_qty": 0,
            "in_progress_qty": 0,
            "total_qty": 0,
            "sizes": {},
        }
    )

    stock_rows = (
        InventoryBatchItem.objects.select_related("item", "color", "size", "batch")
        .filter(
            is_active=True,
            item__is_active=True,
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

        stock_qty = float(row.qty_remaining or 0)
        grouped[key]["stock_qty"] += stock_qty

        size_name = row.size.name if row.size else "-"
        size_sort = row.size.sort_order if row.size else 9999
        size_key = row.size_id or 0

        if size_key not in grouped[key]["sizes"]:
            grouped[key]["sizes"][size_key] = {
                "size_id": row.size_id,
                "size_name": size_name,
                "size_sort": size_sort,
                "stock_qty": 0,
                "available_qty": 0,
                "in_progress_qty": 0,
                "total_qty": 0,
            }

        grouped[key]["sizes"][size_key]["stock_qty"] += stock_qty

    progress_rows = (
        OrderItem.objects.select_related("shirt_item", "color", "size", "order")
        .filter(
            shirt_item__isnull=False,
            shirt_item__is_active=True,
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

        in_progress_qty = Decimal(row.quantity or 0) - Decimal(row.done_qty or 0)
        if in_progress_qty < 0:
            in_progress_qty = Decimal("0")

        in_progress_qty = float(in_progress_qty)
        grouped[key]["in_progress_qty"] += in_progress_qty

        size_name = row.size.name if row.size else "-"
        size_sort = row.size.sort_order if row.size else 9999
        size_key = row.size_id or 0

        if size_key not in grouped[key]["sizes"]:
            grouped[key]["sizes"][size_key] = {
                "size_id": row.size_id,
                "size_name": size_name,
                "size_sort": size_sort,
                "stock_qty": 0,
                "available_qty": 0,
                "in_progress_qty": 0,
                "total_qty": 0,
            }

        grouped[key]["sizes"][size_key]["in_progress_qty"] += in_progress_qty

    variant_cards = []

    for _, data in grouped.items():
        available = _to_int(data.get("stock_qty", 0))
        in_proc = _to_int(data.get("in_progress_qty", 0))

        data["available_qty"] = available
        data["in_progress_qty"] = in_proc
        data["total_qty"] = available + in_proc

        size_list = []

        for _, s in sorted(
            data["sizes"].items(),
            key=lambda x: _shirt_size_sort_key(x[1]),
        ):
            size_available = _to_int(s.get("stock_qty", 0))
            size_in_proc = _to_int(s.get("in_progress_qty", 0))

            s["available_qty"] = size_available
            s["in_progress_qty"] = size_in_proc
            s["total_qty"] = size_available + size_in_proc

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
        cards = sorted(cards, key=_cloth_card_sort_key)

        style_groups.append(
            {
                "style_key": style_key,
                "style_label": cards[0]["item_style_label"],
                "cards": cards,
                "sort_order": style_order.get(style_key, 999),
            }
        )

    style_groups = sorted(style_groups, key=lambda x: x["sort_order"])

    material_types = [
        InventoryItem.TYPE_FILM,
        InventoryItem.TYPE_INK,
        InventoryItem.TYPE_POWDER,
        InventoryItem.TYPE_MAINTENANCE,
        InventoryItem.TYPE_OTHER,
    ]

    material_items = InventoryItem.objects.filter(
        is_active=True,
        item_type__in=material_types,
    ).order_by("item_type", "code", "name")

    material_rows = []

    for material in material_items:
        stock_qty = (
            InventoryBatchItem.objects.filter(
                item=material,
                is_active=True,
                batch__is_deleted=False,
            )
            .aggregate(total=Sum("qty_remaining"))
            .get("total")
            or Decimal("0")
        )

        material.stock_qty = stock_qty
        material_rows.append(material)

    # Existing fabric data stays in Production models, but is displayed inside
    # this same Inventory page so users no longer need a separate stock page.
    fabric_rolls = FabricRoll.objects.select_related("receipt", "receipt__color").all()
    if q:
        fabric_rolls = fabric_rolls.filter(
            Q(roll_code__icontains=q)
            | Q(receipt__fabric_name__icontains=q)
            | Q(receipt__color__name__icontains=q)
            | Q(receipt__supplier__icontains=q)
        )

    fabric_grouped = OrderedDict()
    for roll in fabric_rolls.order_by(
        "receipt__fabric_name", "receipt__color__name", "roll_code"
    ):
        fabric_key = (roll.receipt.fabric_name.strip().lower(), roll.receipt.fabric_name)
        color_key = roll.receipt.color_id
        if fabric_key not in fabric_grouped:
            fabric_grouped[fabric_key] = {
                "fabric_name": roll.receipt.fabric_name,
                "colors": OrderedDict(),
                "roll_count": 0,
                "remaining_total": Decimal("0"),
            }
        fabric = fabric_grouped[fabric_key]
        if color_key not in fabric["colors"]:
            fabric["colors"][color_key] = {
                "color": roll.receipt.color,
                "rolls": [],
                "full_rolls": [],
                "partial_rolls": [],
                "full_count": 0,
                "partial_count": 0,
                "roll_count": 0,
                "remaining_total": Decimal("0"),
                "partial_total": Decimal("0"),
            }

        color_group = fabric["colors"][color_key]
        remaining = Decimal(roll.remaining_qty or 0)

        if remaining <= 0:
            continue

        color_group["rolls"].append(roll)
        color_group["roll_count"] += 1
        color_group["remaining_total"] += remaining
        fabric["roll_count"] += 1
        fabric["remaining_total"] += remaining

        if remaining >= Decimal("1"):
            color_group["full_rolls"].append(roll)
            color_group["full_count"] += 1
        else:
            color_group["partial_rolls"].append(roll)
            color_group["partial_count"] += 1
            color_group["partial_total"] += remaining

    fabric_groups = []
    for fabric in fabric_grouped.values():
        fabric["colors"] = list(fabric["colors"].values())
        fabric_groups.append(fabric)

    batch_rows = []

    for batch in batches:
        total_cloth = 0

        for item in batch.items.all():
            if item.item and item.item.item_type == InventoryItem.TYPE_SHIRT:
                total_cloth += _to_int(item.qty_received or 0)

        batch_rows.append(
            {
                "id": batch.id,
                "batch_no": batch.batch_no,
                "supplier": batch.supplier_name or "-",
                "created_by": batch.created_by.username if batch.created_by else "-",
                "received_date": batch.received_date,
                "total_cloth": total_cloth,
            }
        )


    # Latest 5 Stock In activities across Cloth, Printing Material and Fabric.
    recent_stock_activities = []

    recent_batches = (
        InventoryBatch.objects
        .select_related("created_by", "supplier_ref")
        .prefetch_related("items__item")
        .filter(is_deleted=False)
        .order_by("-received_date", "-created_at", "-id")[:10]
    )

    for recent_batch in recent_batches:
        cloth_qty = Decimal("0")
        material_qty = Decimal("0")
        material_names = []

        for row in recent_batch.items.all():
            if not row.item:
                continue

            qty = Decimal(row.qty_received or 0)

            if row.item.item_type == InventoryItem.TYPE_SHIRT:
                cloth_qty += qty
            else:
                material_qty += qty

                if row.item.name not in material_names:
                    material_names.append(row.item.name)

        # A mixed batch is shown as Cloth when it contains cloth.
        if cloth_qty > 0:
            activity_type = "Cloth"
            quantity = cloth_qty
            unit = "shirt" if cloth_qty == 1 else "shirts"
            item_summary = "Cloth Stock In"
        else:
            activity_type = "Printing Material"
            quantity = material_qty
            unit = "unit" if material_qty == 1 else "units"
            item_summary = (
                ", ".join(material_names[:3])
                or "Printing Material Stock In"
            )

        recent_stock_activities.append(
            {
                "type": activity_type,
                "reference": recent_batch.batch_no,
                "supplier": recent_batch.supplier_name or "-",
                "quantity": quantity,
                "unit": unit,
                "summary": item_summary,
                "received_date": recent_batch.received_date,
                "created_at": recent_batch.created_at,
                "created_by": (
                    recent_batch.created_by.username
                    if recent_batch.created_by
                    else "-"
                ),
                "detail_id": recent_batch.pk,
            }
        )

    recent_receipts = (
        FabricReceipt.objects
        .select_related(
            "fabric_type",
            "color",
            "created_by",
            "supplier_ref",
        )
        .order_by("-received_date", "-created_at", "-id")[:10]
    )

    for receipt in recent_receipts:
        roll_count = Decimal(receipt.roll_count or 0)

        recent_stock_activities.append(
            {
                "type": "Fabric",
                "reference": receipt.receipt_no,
                "supplier": receipt.supplier or "-",
                "quantity": roll_count,
                "unit": "roll" if roll_count == 1 else "rolls",
                "summary": (
                    receipt.fabric_name
                    + (
                        f" · {receipt.color.name}"
                        if receipt.color_id
                        else ""
                    )
                ),
                "received_date": receipt.received_date,
                "created_at": receipt.created_at,
                "created_by": (
                    receipt.created_by.username
                    if receipt.created_by
                    else "-"
                ),
                "detail_id": receipt.pk,
            }
        )

    recent_stock_activities.sort(
        key=lambda row: (
            row.get("received_date") or timezone.localdate(),
            row.get("created_at") or timezone.now(),
        ),
        reverse=True,
    )

    recent_stock_activities = recent_stock_activities[:5]

    return render(
        request,
        "inventory/inventory_list.html",
        {
            "items": items,
            "style_groups": style_groups,
            "materials": material_rows,
            "fabric_groups": fabric_groups,
            "inventory_type": inventory_type,
            "q": q,
            "batches": batch_rows,
            "recent_stock_activities": recent_stock_activities,
        },
    )


@login_required
@permission_required("inventory.view_inventoryitem", raise_exception=True)
def inventory_item_list(request):
    items = InventoryItem.objects.filter(is_active=True).order_by("code", "name")
    return render(request, "inventory/inventory_item_list.html", {"items": items})


@login_required
@permission_required("inventory.add_inventoryitem", raise_exception=True)
def inventory_item_create(request):
    if request.method == "POST":
        form = InventoryItemForm(request.POST, request.FILES)
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
        form = InventoryItemForm(request.POST, request.FILES, instance=item)
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
            "object": item,
            "page_title": "Edit Item",
            "submit_label": "Update Item",
        },
    )


@login_required
@permission_required("inventory.delete_inventoryitem", raise_exception=True)
def inventory_item_delete(request, pk):
    item = get_object_or_404(InventoryItem, pk=pk)

    used_in_batch = InventoryBatchItem.objects.filter(item=item).exists()
    used_in_order = OrderItem.objects.filter(
        Q(shirt_item=item) | Q(film_item=item) | Q(material_item=item)
    ).exists()

    if used_in_batch or used_in_order:
        messages.error(request, "Cannot delete. Item already used in stock or orders.")
        return redirect("inventory_item_list")

    if request.method == "POST":
        item.delete()
        messages.success(request, "Item deleted successfully.")
        return redirect("inventory_item_list")

    return render(request, "inventory/inventory_item_delete.html", {"item": item})


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


def _form_error_payload(form, formset):
    field_errors = {}
    for name, errors in form.errors.items():
        field_errors[f"id_{name}"] = [str(error) for error in errors]
    row_errors = {}
    for index, row_form in enumerate(formset.forms):
        for name, errors in row_form.errors.items():
            row_errors[f"id_{formset.prefix}-{index}-{name}"] = [str(error) for error in errors]
    first = next(iter(field_errors.values()), None) or next(iter(row_errors.values()), None)
    message = first[0] if first else "Please check the highlighted fields."
    return {"ok": False, "message": f"Failed: {message}", "field_errors": field_errors, "row_errors": row_errors}


@login_required
@permission_required("production.add_productionsupplier", raise_exception=True)
def inventory_supplier_ajax_create(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "Failed: POST request required."}, status=405)
    name = (request.POST.get("name") or "").strip()
    phone = (request.POST.get("phone") or "").strip()
    location = (request.POST.get("location") or "").strip()
    if not name:
        return JsonResponse({"ok": False, "message": "Failed: Supplier name is required.", "field": "supplier-modal-name"}, status=400)
    existing = ProductionSupplier.objects.filter(name__iexact=name).first()
    if existing:
        return JsonResponse({"ok": True, "supplier": {"id": existing.pk, "name": existing.name}, "message": "Supplier already exists and was selected."})
    supplier = ProductionSupplier.objects.create(name=name, phone=phone, location=location)
    return JsonResponse({"ok": True, "supplier": {"id": supplier.pk, "name": supplier.name}, "message": "Supplier created successfully."})


def _create_pending_inventory_expense(batch, user):
    expense, created = Expense.objects.get_or_create(
        expense_type=Expense.TYPE_BATCH,
        batch=batch,
        defaults={
            "created_by": user,
            "amount": Decimal("0.00"),
            "batch_created_at": batch.created_at,
            "batch_total_cloth": int(batch.total_cloth or 0),
            "batch_cost": Decimal("0.00"),
            "batch_delivery_fee": Decimal("0.00"),
            "batch_other_fee": Decimal("0.00"),
            "expense_status": Expense.STATUS_PENDING,
            "stock_source_type": Expense.SOURCE_INVENTORY,
            "source_reference": batch.batch_no,
            "supplier_name": batch.supplier_name,
            "received_date": batch.received_date,
            "note": "Auto-created from Stock In. Cost pending.",
        },
    )
    return expense


def _create_pending_fabric_expense(receipts, supplier, received_date, user):
    """
    Create one Finance expense for a Fabric Stock In group.

    If cost was entered during Stock In, the Finance record is immediately
    COMPLETED. If no cost was entered, it stays PENDING so Finance can fill it later.
    """
    if not receipts:
        return None

    refs = [receipt.receipt_no for receipt in receipts if receipt.receipt_no]
    reference = refs[0] if len(refs) == 1 else f"{refs[0]} +{len(refs)-1}"

    goods_cost = sum(
        (Decimal(receipt.total_goods_cost or 0) for receipt in receipts),
        Decimal("0.00"),
    )
    delivery_fee = sum(
        (Decimal(receipt.shipping_cost or 0) for receipt in receipts),
        Decimal("0.00"),
    )
    other_fee = sum(
        (Decimal(receipt.extra_cost or 0) for receipt in receipts),
        Decimal("0.00"),
    )
    total_amount = goods_cost + delivery_fee + other_fee
    cost_added = total_amount > 0

    return Expense.objects.create(
        expense_type=Expense.TYPE_BATCH,
        created_by=user,
        amount=total_amount,
        batch_cost=goods_cost,
        batch_delivery_fee=delivery_fee,
        batch_other_fee=other_fee,
        expense_status=(
            Expense.STATUS_COMPLETED
            if cost_added
            else Expense.STATUS_PENDING
        ),
        stock_source_type=Expense.SOURCE_FABRIC,
        source_reference=reference,
        supplier_name=supplier.name if supplier else "",
        received_date=received_date,
        fabric_receipt_ids=[receipt.pk for receipt in receipts],
        note=(
            "Cost added during Fabric Stock In."
            if cost_added
            else "Auto-created from Fabric Stock In. Cost pending."
        ),
    )


def _allocate_fabric_batch_cost(receipts, goods_cost, shipping_cost, extra_cost):
    """
    Allocate one batch-level Fabric cost across the created FabricReceipt rows
    by roll count. This preserves total cost exactly while keeping each receipt's
    own cost/cost-per-roll usable by Production and Finance.
    """
    if not receipts:
        return

    goods_cost = Decimal(goods_cost or 0)
    shipping_cost = Decimal(shipping_cost or 0)
    extra_cost = Decimal(extra_cost or 0)

    total_rolls = sum(
        (Decimal(receipt.roll_count or 0) for receipt in receipts),
        Decimal("0"),
    )

    if total_rolls <= 0:
        total_rolls = Decimal(len(receipts) or 1)
        weights = [Decimal("1") for _ in receipts]
    else:
        weights = [Decimal(receipt.roll_count or 0) for receipt in receipts]

    remaining_goods = goods_cost
    remaining_shipping = shipping_cost
    remaining_extra = extra_cost

    for index, receipt in enumerate(receipts):
        is_last = index == len(receipts) - 1

        if is_last:
            row_goods = remaining_goods
            row_shipping = remaining_shipping
            row_extra = remaining_extra
        else:
            weight = weights[index]
            row_goods = (goods_cost * weight / total_rolls).quantize(Decimal("0.01"))
            row_shipping = (shipping_cost * weight / total_rolls).quantize(Decimal("0.01"))
            row_extra = (extra_cost * weight / total_rolls).quantize(Decimal("0.01"))

            remaining_goods -= row_goods
            remaining_shipping -= row_shipping
            remaining_extra -= row_extra

        receipt.total_goods_cost = row_goods
        receipt.shipping_cost = row_shipping
        receipt.extra_cost = row_extra
        receipt.save(
            update_fields=[
                "total_goods_cost",
                "shipping_cost",
                "extra_cost",
                "updated_at",
            ]
        )


@login_required
@permission_required("inventory.add_inventorybatch", raise_exception=True)
@transaction.atomic
def inventory_batch_create(request):
    stock_type = (request.POST.get("stock_type") or request.GET.get("type") or "cloth").strip().lower()
    if stock_type not in {"cloth", "printing", "fabric"}:
        stock_type = "cloth"

    # Fabric uses the same two clear actions as other purchased stock:
    # Stock In Now = create physical rolls immediately.
    # Save Purchase = save the order/cost only; rolls are created only after Confirm Received.
    if stock_type == "fabric":
        if request.method == "POST":
            fabric_header_form = FabricReceiptHeaderForm(request.POST)
            fabric_formset = fabric_receipt_line_formset(
                data=request.POST, user=request.user, prefix="fabric_items"
            )
            if fabric_header_form.is_valid() and fabric_formset.is_valid():
                submit_action = (request.POST.get("submit_action") or "stock_in_now").strip()
                is_purchase = submit_action == "save_purchase"

                try:
                    fabric_goods_cost = Decimal(request.POST.get("fabric_goods_cost") or "0")
                    fabric_shipping_cost = Decimal(request.POST.get("fabric_shipping_cost") or "0")
                    fabric_extra_cost = Decimal(request.POST.get("fabric_extra_cost") or "0")
                except Exception:
                    fabric_goods_cost = fabric_shipping_cost = fabric_extra_cost = Decimal("-1")

                # Purchased stock must carry its purchase cost. Production Stock In never uses this form.
                if fabric_goods_cost <= 0 or fabric_shipping_cost < 0 or fabric_extra_cost < 0:
                    message = "Failed: Goods Cost is required and must be greater than 0. Delivery/Extra Cost can be 0."
                    if request.headers.get("x-requested-with") == "XMLHttpRequest":
                        transaction.set_rollback(True)
                        return JsonResponse({"ok": False, "message": message}, status=400)
                    messages.error(request, message)
                else:
                    total_rolls = 0
                    saved_lines = 0
                    created_receipts = []
                    group_ref = f"FP-{timezone.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6].upper()}"
                    supplier = fabric_header_form.cleaned_data["supplier"]
                    planned_date = fabric_header_form.cleaned_data["received_date"]

                    for line_form in fabric_formset:
                        if not line_form.cleaned_data or line_form.cleaned_data.get("DELETE"):
                            continue
                        data = line_form.cleaned_data
                        fabric_type = data["fabric_type"]
                        weights = data.get("roll_weights_list") or []
                        safe_weights = [str(Decimal(weight)) for weight in weights]

                        receipt = FabricReceipt(
                            received_date=planned_date,
                            expected_date=planned_date if is_purchase else None,
                            status=FabricReceipt.STATUS_WAITING if is_purchase else FabricReceipt.STATUS_RECEIVED,
                            purchase_group=group_ref,
                            pending_roll_weights=safe_weights if is_purchase else [],
                            received_at=None if is_purchase else timezone.now(),
                            received_by=None if is_purchase else request.user,
                            supplier_ref=supplier,
                            supplier=supplier.name,
                            fabric_type=fabric_type,
                            fabric_name=fabric_type.name,
                            color=data["color"],
                            roll_count=data["roll_count"],
                            total_goods_cost=Decimal("0"),
                            shipping_cost=Decimal("0"),
                            extra_cost=Decimal("0"),
                            note=data.get("note") or "",
                            created_by=request.user,
                            updated_by=request.user,
                        )
                        receipt.save()
                        if not is_purchase:
                            create_fabric_rolls(receipt, weights)
                        created_receipts.append(receipt)
                        total_rolls += receipt.roll_count
                        saved_lines += 1

                    _allocate_fabric_batch_cost(
                        created_receipts,
                        fabric_goods_cost,
                        fabric_shipping_cost,
                        fabric_extra_cost,
                    )
                    try:
                        _create_pending_fabric_expense(
                            created_receipts, supplier, planned_date, request.user
                        )
                    except Exception:
                        logger.exception("Fabric purchase saved, but Finance expense sync failed.")

                    if is_purchase:
                        message = (
                            f"Fabric purchase saved: {total_rolls} roll(s) across {saved_lines} fabric type(s). "
                            "Stock has not increased. Confirm Received when the fabric arrives."
                        )
                    else:
                        message = (
                            f"{total_rolls} fabric roll(s) across {saved_lines} fabric type(s) received. "
                            "Fabric stock increased successfully."
                        )

                    if request.headers.get("x-requested-with") == "XMLHttpRequest":
                        return JsonResponse({
                            "ok": True,
                            "message": message,
                            "redirect_url": "/inventory/stock-in-list/",
                        })
                    messages.success(request, message)
                    return redirect("inventory_stock_in_list")

            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                transaction.set_rollback(True)
                field_errors = {
                    f"id_{name}": [str(e) for e in errors]
                    for name, errors in fabric_header_form.errors.items()
                }
                row_errors = {}
                for index, row_form in enumerate(fabric_formset.forms):
                    for name, errors in row_form.errors.items():
                        row_errors[f"id_fabric_items-{index}-{name}"] = [str(e) for e in errors]
                first = next(iter(field_errors.values()), None) or next(iter(row_errors.values()), None)
                message = first[0] if first else "Please check the highlighted fields."
                return JsonResponse({
                    "ok": False,
                    "message": f"Failed: {message}",
                    "field_errors": field_errors,
                    "row_errors": row_errors,
                }, status=400)
        else:
            fabric_header_form = FabricReceiptHeaderForm(initial={"received_date": timezone.localdate()})
            fabric_formset = fabric_receipt_line_formset(user=request.user, prefix="fabric_items")

        form = InventoryBatchForm(initial={"received_date": timezone.localdate()})
        formset = InventoryBatchItemFormSet(prefix="items")
        return render(request, "inventory/inventory_batch_form.html", {
            "form": form,
            "formset": formset,
            "fabric_header_form": fabric_header_form,
            "fabric_formset": fabric_formset,
            "stock_type": "fabric",
            "page_title": "Stock In",
            "submit_label": "Stock In Now",
            "items": InventoryItem.objects.filter(is_active=True).order_by("code", "name"),
            "can_view_stock_cost": _can_view_stock_cost(request.user),
        })

    # Cloth / Printing Material
    if request.method == "POST":
        form = InventoryBatchForm(request.POST)
        formset = InventoryBatchItemFormSet(request.POST, prefix="items")
        if form.is_valid() and formset.is_valid():
            submit_action = (request.POST.get("submit_action") or "stock_in_now").strip()
            is_purchase = submit_action == "save_purchase"

            batch = form.save(commit=False)
            if Decimal(batch.total_goods_cost or 0) <= 0:
                message = "Failed: Goods Cost is required and must be greater than 0. Delivery/Extra Cost can be 0."
                if request.headers.get("x-requested-with") == "XMLHttpRequest":
                    transaction.set_rollback(True)
                    return JsonResponse({"ok": False, "message": message}, status=400)
                messages.error(request, message)
                return render(request, "inventory/inventory_batch_form.html", {
                    "form": form, "formset": formset,
                    "fabric_header_form": FabricReceiptHeaderForm(initial={"received_date": timezone.localdate()}),
                    "fabric_formset": fabric_receipt_line_formset(user=request.user, prefix="fabric_items"),
                    "stock_type": stock_type, "page_title": "Stock In", "submit_label": "Stock In Now",
                    "items": InventoryItem.objects.filter(is_active=True).order_by("code", "name"),
                    "can_view_stock_cost": _can_view_stock_cost(request.user),
                })

            batch.status = InventoryBatch.STATUS_COMING_SOON if is_purchase else InventoryBatch.STATUS_RECEIVED
            batch.expected_date = form.cleaned_data.get("received_date") if is_purchase else None
            batch.received_at = None if is_purchase else timezone.now()
            batch.received_by = None if is_purchase else request.user

            # Cost entry is optional during Stock In.
            # If staff entered any cost amount, Finance is completed immediately.
            entered_cost_total = (
                Decimal(batch.total_goods_cost or 0)
                + Decimal(batch.shipping_cost or 0)
                + Decimal(batch.extra_cost or 0)
            )
            cost_was_added = (
                request.POST.get("cost_is_added") == "1"
                or entered_cost_total > 0
            )

            if cost_was_added:
                batch.cost_is_added = True
                batch.cost_added_at = timezone.now()
                batch.cost_added_by = request.user
            else:
                batch.total_goods_cost = Decimal("0.00")
                batch.shipping_cost = Decimal("0.00")
                batch.extra_cost = Decimal("0.00")
                batch.cost_is_added = False
                batch.cost_added_at = None
                batch.cost_added_by = None

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
                item.base_unit_cost = Decimal("0")
                item.final_unit_cost = Decimal("0")
                item.is_active = True
                # qty_received is the ordered/expected quantity.
                # A saved purchase has not physically arrived yet.
                item.qty_arrived = Decimal("0") if is_purchase else Decimal(item.qty_received or 0)
                item.qty_remaining = Decimal("0") if is_purchase else Decimal(item.qty_received or 0)
                item.save()
                if not is_purchase:
                    log_stock_in(
                        batch_item=item,
                        qty_before=Decimal("0"),
                        qty_after=item.qty_remaining,
                        batch=batch,
                        user=request.user,
                        remark=f"Stock in from batch {batch.batch_no}",
                    )

            _log_batch_history(
                batch,
                InventoryBatchHistory.ACTION_CREATE,
                request.user,
                "Purchase saved - waiting to receive" if is_purchase else "Stock In received",
            )
            try:
                _sync_inventory_batch_expense(batch, request.user)
            except Exception:
                logger.exception("Stock In saved, but Finance expense sync failed.")

            if is_purchase:
                message = f"Purchase {batch.batch_no} saved as Coming Soon. Stock has not increased."
            else:
                message = f"Inventory batch {batch.batch_no} received and stock added successfully."

            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"ok": True, "message": message, "redirect_url": "/inventory/stock-in-list/"})
            messages.success(request, message)
            return redirect("inventory_batch_detail", pk=batch.pk)

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            transaction.set_rollback(True)
            field_errors = {f"id_{name}": [str(e) for e in errors] for name, errors in form.errors.items()}
            row_errors = {}
            for index, row_form in enumerate(formset.forms):
                for name, errors in row_form.errors.items():
                    row_errors[f"id_items-{index}-{name}"] = [str(e) for e in errors]
            first = next(iter(field_errors.values()), None) or next(iter(row_errors.values()), None)
            message = first[0] if first else "Please check the highlighted fields."
            return JsonResponse({"ok": False, "message": f"Failed: {message}", "field_errors": field_errors, "row_errors": row_errors}, status=400)
    else:
        form = InventoryBatchForm(initial={"received_date": timezone.localdate()})
        formset = InventoryBatchItemFormSet(prefix="items")

    fabric_header_form = FabricReceiptHeaderForm(initial={"received_date": timezone.localdate()})
    fabric_formset = fabric_receipt_line_formset(user=request.user, prefix="fabric_items")
    return render(request, "inventory/inventory_batch_form.html", {
        "form": form,
        "formset": formset,
        "fabric_header_form": fabric_header_form,
        "fabric_formset": fabric_formset,
        "stock_type": stock_type,
        "page_title": "Stock In",
        "submit_label": "Stock In Now",
        "items": InventoryItem.objects.filter(is_active=True).order_by("code", "name"),
        "can_view_stock_cost": _can_view_stock_cost(request.user),
    })


def _backfill_legacy_received_stock_in():
    """
    One-time compatibility repair for Stock In records created before the new
    Purchase / Receive Later workflow was introduced on 24 Aug 2026.

    Before that date every InventoryBatch represented stock that had already
    physically arrived. During the workflow upgrade those old rows can inherit
    the new Waiting Arrival status / zero qty_arrived defaults.

    IMPORTANT: qty_remaining is deliberately NEVER changed here because it is
    the current live stock balance and may already be lower after usage/orders.
    """
    cutoff = date(2026, 8, 24)

    legacy_batches = InventoryBatch.objects.filter(
        created_at__date__lt=cutoff,
        is_deleted=False,
    ).exclude(status=InventoryBatch.STATUS_RECEIVED)

    legacy_batch_ids = legacy_batches.values_list("pk", flat=True)

    # Old Stock In quantities had already arrived physically. Copy the original
    # received/ordered quantity into qty_arrived, but keep qty_remaining intact.
    InventoryBatchItem.objects.filter(batch_id__in=legacy_batch_ids).update(
        qty_arrived=F("qty_received")
    )

    # Mark the legacy batches received. Preserve their old dates; only populate
    # the new receive metadata when those fields are empty.
    legacy_batches.filter(received_at__isnull=True).update(
        received_at=F("created_at")
    )
    legacy_batches.filter(
        received_by__isnull=True,
        created_by__isnull=False,
    ).update(received_by_id=F("created_by_id"))
    legacy_batches.update(status=InventoryBatch.STATUS_RECEIVED)


def _fabric_purchase_groups(receipts, status_kind):
    """Group FabricReceipt children by purchase_group for one batch-style Stock In row."""
    grouped = OrderedDict()
    for receipt in receipts:
        key = receipt.purchase_group or f"legacy-{receipt.pk}"
        group = grouped.setdefault(key, {
            "key": key,
            "receipts": [],
            "anchor": receipt,
            "total_rolls": Decimal("0"),
            "total_kg": Decimal("0"),
            "total_cost": Decimal("0"),
        })
        group["receipts"].append(receipt)
        group["total_rolls"] += Decimal(receipt.roll_count or 0)
        group["total_kg"] += Decimal(getattr(receipt, "total_kg_display", 0) or 0)
        group["total_cost"] += Decimal(receipt.total_cost or 0)
        if receipt.pk < group["anchor"].pk:
            group["anchor"] = receipt

    rows = []
    for group in grouped.values():
        receipts = sorted(group["receipts"], key=lambda r: (r.receipt_no or "", r.pk))
        anchor = group["anchor"]
        row_date = (
            (anchor.expected_date or anchor.received_date)
            if status_kind == "waiting"
            else anchor.received_date
        )
        children = []
        for receipt in receipts:
            children.append({
                "ref": receipt.receipt_no,
                "fabric": receipt.fabric_name or "-",
                "color": receipt.color.name if receipt.color else "-",
                "color_hex": getattr(receipt.color, "hex_code", "#D1D5DB") if receipt.color else "#D1D5DB",
                "rolls": Decimal(receipt.roll_count or 0),
                "kg": Decimal(getattr(receipt, "total_kg_display", 0) or 0),
                "cost": Decimal(receipt.total_cost or 0),
                "obj": receipt,
            })
        rows.append({
            "kind": "fabric_group",
            "sort_date": row_date or anchor.created_at.date(),
            "ref": receipts[0].receipt_no if receipts else anchor.receipt_no,
            "purchase_group": anchor.purchase_group or "",
            "date": row_date,
            "supplier": anchor.supplier or "-",
            "type": "Fabric",
            "item": f"{len(children)} fabric type(s)",
            "ordered": group["total_rolls"],
            "arrived": Decimal("0") if status_kind == "waiting" else group["total_rolls"],
            "waiting": group["total_rolls"] if status_kind == "waiting" else Decimal("0"),
            "qty": group["total_rolls"],
            "qty_label": f"{group['total_kg']:.2f} KG",
            "status": "waiting" if status_kind == "waiting" else "received",
            "cost_added": bool(group["total_cost"] > 0),
            "cost_text": f"$ {group['total_cost']:.2f}",
            "created_by": anchor.created_by.username if anchor.created_by else "-",
            "obj": anchor,
            "children": children,
            "child_count": len(children),
            "print_ready": True,
        })
    return rows


def inventory_stock_in_list(request):
    """Stock In List grouped into Waiting Purchase, Purchased Stock In, and Production Stock."""
    # Safe one-time legacy repair. After the first successful page load there
    # are no matching rows, so this becomes a no-op.
    _backfill_legacy_received_stock_in()

    # Default view is WAITING so staff immediately see stock that still needs
    # to be received. The three operational groups are:
    #   waiting    = supplier purchases not fully received yet
    #   purchased  = supplier purchases already received into stock
    #   production = stock created automatically from Production
    # "all" is kept as a useful search option.
    list_type = (request.GET.get("list_type") or "waiting").strip().lower()
    if list_type not in {"waiting", "purchased", "production", "all"}:
        list_type = "waiting"

    q = (request.GET.get("q") or "").strip()
    from_date_raw = (request.GET.get("from_date") or "").strip()
    to_date_raw = (request.GET.get("to_date") or "").strip()
    cost_status = (request.GET.get("cost_status") or "all").strip().lower()
    if cost_status not in {"all", "missing", "added"}:
        cost_status = "all"

    material_type = (request.GET.get("material_type") or "all").strip().lower()
    if material_type not in {"all", "cloth", "printing", "fabric"}:
        material_type = "all"

    def parse_filter_date(value):
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    from_date = parse_filter_date(from_date_raw)
    to_date = parse_filter_date(to_date_raw)

    base_batches = (
        InventoryBatch.objects
        .filter(is_deleted=False)
        .select_related(
            "supplier_ref", "created_by", "received_by", "cost_added_by",
            "production_sewing_return__job__project",
            "production_sewing_return__job__project_color__color",
        )
        .prefetch_related("items__item", "items__color", "items__size")
    )

    purchase_base_qs = base_batches.filter(production_sewing_return__isnull=True)
    production_qs = base_batches.filter(production_sewing_return__isnull=False)

    # Cloth + Printing Material purchase rows are split by operational group.
    waiting_batches_qs = purchase_base_qs.filter(
        status__in=[InventoryBatch.STATUS_COMING_SOON, InventoryBatch.STATUS_PARTIAL]
    )
    purchased_batches_qs = purchase_base_qs.filter(status=InventoryBatch.STATUS_RECEIVED)

    # Date meaning follows the selected stock group:
    # Waiting Purchase = expected date; Purchased/Production = received date.
    # Legacy waiting records may have no expected_date, so received_date is used as fallback.
    if from_date:
        waiting_batches_qs = waiting_batches_qs.filter(
            Q(expected_date__gte=from_date)
            | Q(expected_date__isnull=True, received_date__gte=from_date)
        )
        purchased_batches_qs = purchased_batches_qs.filter(received_date__gte=from_date)
        production_qs = production_qs.filter(received_date__gte=from_date)
    if to_date:
        waiting_batches_qs = waiting_batches_qs.filter(
            Q(expected_date__lte=to_date)
            | Q(expected_date__isnull=True, received_date__lte=to_date)
        )
        purchased_batches_qs = purchased_batches_qs.filter(received_date__lte=to_date)
        production_qs = production_qs.filter(received_date__lte=to_date)

    # Optional cost-status filter for supplier purchases. Production stock has no
    # separate purchase cost, so this filter intentionally applies only to purchases.
    if cost_status == "missing":
        waiting_batches_qs = waiting_batches_qs.filter(cost_is_added=False)
        purchased_batches_qs = purchased_batches_qs.filter(cost_is_added=False)
    elif cost_status == "added":
        waiting_batches_qs = waiting_batches_qs.filter(cost_is_added=True)
        purchased_batches_qs = purchased_batches_qs.filter(cost_is_added=True)

    if q:
        batch_filter = (
            Q(batch_no__icontains=q)
            | Q(supplier__icontains=q)
            | Q(supplier_ref__name__icontains=q)
            | Q(items__item__name__icontains=q)
            | Q(items__item__code__icontains=q)
        )
        waiting_batches_qs = waiting_batches_qs.filter(batch_filter).distinct()
        purchased_batches_qs = purchased_batches_qs.filter(batch_filter).distinct()
        production_qs = production_qs.filter(batch_filter).distinct()

    def decorate_batch(batch):
        item_names = []
        total_qty = Decimal("0")
        has_cloth = False
        has_printing = False
        for row in batch.items.all():
            if not row.item:
                continue
            total_qty += Decimal(row.qty_received or 0)
            if row.item.name not in item_names:
                item_names.append(row.item.name)
            if row.item.item_type == InventoryItem.TYPE_SHIRT:
                has_cloth = True
            else:
                has_printing = True
        if has_cloth and has_printing:
            batch.stock_type_label = "Mixed"
        elif has_cloth:
            batch.stock_type_label = "Cloth"
        else:
            batch.stock_type_label = "Printing Material"
        batch.stock_item_summary = ", ".join(item_names[:3]) or "-"
        if len(item_names) > 3:
            batch.stock_item_summary += f" +{len(item_names) - 3}"
        batch.stock_total_qty = total_qty
        batch.stock_arrived_qty = sum(
            (Decimal(r.qty_arrived or 0) for r in batch.items.all()), Decimal("0")
        )
        batch.stock_pending_qty = max(batch.stock_total_qty - batch.stock_arrived_qty, Decimal("0"))

    waiting_batches = list(waiting_batches_qs.order_by("-created_at", "-id")[:500])
    purchased_batches = list(purchased_batches_qs.order_by("-created_at", "-id")[:500])
    production_batches = list(production_qs.order_by("-created_at", "-id")[:500])

    for batch in waiting_batches + purchased_batches + production_batches:
        decorate_batch(batch)

    # Fabric purchases use FabricReceipt instead of InventoryBatch, but are
    # placed in the same Waiting/Purchased groups so staff do not need to
    # search a separate Fabric section first.
    fabric_base_qs = (
        FabricReceipt.objects
        .select_related("fabric_type", "color", "supplier_ref", "created_by", "received_by")
        .prefetch_related("rolls")
    )

    waiting_fabric_qs = fabric_base_qs.filter(status=FabricReceipt.STATUS_WAITING)
    purchased_fabric_qs = fabric_base_qs.filter(status=FabricReceipt.STATUS_RECEIVED)

    if from_date:
        waiting_fabric_qs = waiting_fabric_qs.filter(
            Q(expected_date__gte=from_date)
            | Q(expected_date__isnull=True, received_date__gte=from_date)
        )
        purchased_fabric_qs = purchased_fabric_qs.filter(received_date__gte=from_date)
    if to_date:
        waiting_fabric_qs = waiting_fabric_qs.filter(
            Q(expected_date__lte=to_date)
            | Q(expected_date__isnull=True, received_date__lte=to_date)
        )
        purchased_fabric_qs = purchased_fabric_qs.filter(received_date__lte=to_date)

    # Fabric purchases do not use InventoryBatch.cost_is_added. New fabric
    # purchases always carry total_cost, while legacy missing-cost records are 0.
    if cost_status == "missing":
        waiting_fabric_qs = waiting_fabric_qs.filter(total_cost__lte=0)
        purchased_fabric_qs = purchased_fabric_qs.filter(total_cost__lte=0)
    elif cost_status == "added":
        waiting_fabric_qs = waiting_fabric_qs.filter(total_cost__gt=0)
        purchased_fabric_qs = purchased_fabric_qs.filter(total_cost__gt=0)

    if q:
        fabric_filter = (
            Q(receipt_no__icontains=q)
            | Q(purchase_group__icontains=q)
            | Q(supplier__icontains=q)
            | Q(supplier_ref__name__icontains=q)
            | Q(fabric_name__icontains=q)
            | Q(color__name__icontains=q)
            | Q(rolls__roll_code__icontains=q)
        )
        waiting_fabric_qs = waiting_fabric_qs.filter(fabric_filter).distinct()
        purchased_fabric_qs = purchased_fabric_qs.filter(fabric_filter).distinct()

    waiting_fabric_receipts = list(waiting_fabric_qs.order_by("-created_at", "-id")[:500])
    purchased_fabric_receipts = list(purchased_fabric_qs.order_by("-created_at", "-id")[:500])

    def decorate_fabric_receipt(receipt):
        receipt.total_kg_display = sum(
            (Decimal(roll.original_qty or 0) for roll in receipt.rolls.all()), Decimal("0")
        )
        if receipt.status == FabricReceipt.STATUS_WAITING:
            receipt.total_kg_display = sum(
                (Decimal(str(x)) for x in (receipt.pending_roll_weights or []) if str(x).strip()),
                Decimal("0"),
            )

    for receipt in waiting_fabric_receipts + purchased_fabric_receipts:
        decorate_fabric_receipt(receipt)

    # Combine Cloth, Printing Material and Fabric into one operational table.
    # This keeps staff from having to read separate sub-sections.
    waiting_rows = []
    purchased_rows = []

    for batch in waiting_batches:
        row_date = batch.expected_date or batch.received_date
        waiting_rows.append({
            "kind": "batch",
            "sort_date": row_date or batch.created_at.date(),
            "ref": batch.batch_no,
            "date": row_date,
            "supplier": batch.supplier_name or "-",
            "type": batch.stock_type_label,
            "item": batch.stock_item_summary,
            "ordered": batch.stock_total_qty,
            "arrived": batch.stock_arrived_qty,
            "waiting": batch.stock_pending_qty,
            "qty_label": "",
            "status": "partial" if batch.status == InventoryBatch.STATUS_PARTIAL else "waiting",
            "cost_added": bool(batch.cost_is_added),
            "cost_text": "Cost Added" if batch.cost_is_added else "Cost Missing",
            "created_by": batch.created_by.username if batch.created_by else "-",
            "obj": batch,
        })

    waiting_rows.extend(_fabric_purchase_groups(waiting_fabric_receipts, "waiting"))

    for batch in purchased_batches:
        purchased_rows.append({
            "kind": "batch",
            "sort_date": batch.received_date or batch.created_at.date(),
            "ref": batch.batch_no,
            "date": batch.received_date,
            "supplier": batch.supplier_name or "-",
            "type": batch.stock_type_label,
            "item": batch.stock_item_summary,
            "qty": batch.stock_arrived_qty,
            "qty_label": "",
            "cost_added": bool(batch.cost_is_added),
            "cost_text": "Cost Added" if batch.cost_is_added else "Old Record: Cost Missing",
            "created_by": batch.created_by.username if batch.created_by else "-",
            "obj": batch,
        })

    purchased_rows.extend(_fabric_purchase_groups(purchased_fabric_receipts, "received"))

    # Optional material-type filter across the combined list.
    # Fabric is stored in FabricReceipt; Cloth/Printing Material are InventoryBatch rows.
    if material_type != "all":
        wanted_label = {
            "cloth": "Cloth",
            "printing": "Printing Material",
            "fabric": "Fabric",
        }[material_type]

        def row_matches_material(row):
            row_type = row.get("type")
            if row_type == wanted_label:
                return True
            # A legacy Mixed InventoryBatch contains both Cloth and Printing Material.
            if row_type == "Mixed" and material_type in {"cloth", "printing"}:
                return True
            return False

        waiting_rows = [row for row in waiting_rows if row_matches_material(row)]
        purchased_rows = [row for row in purchased_rows if row_matches_material(row)]

        # Production rows use InventoryBatch and have already been decorated.
        if material_type == "fabric":
            production_batches = []
        elif material_type == "cloth":
            production_batches = [
                batch for batch in production_batches
                if batch.stock_type_label in {"Cloth", "Mixed"}
            ]
        elif material_type == "printing":
            production_batches = [
                batch for batch in production_batches
                if batch.stock_type_label in {"Printing Material", "Mixed"}
            ]

    waiting_rows.sort(
        key=lambda row: (row["sort_date"], row["ref"]),
        reverse=True,
    )
    purchased_rows.sort(
        key=lambda row: (row["sort_date"], row["ref"]),
        reverse=True,
    )

    # Only send the selected operational group to the template, except "all"
    # which intentionally shows all three groups.
    show_waiting = list_type in {"waiting", "all"}
    show_purchased = list_type in {"purchased", "all"}
    show_production = list_type in {"production", "all"}

    return render(request, "inventory/stock_in_list.html", {
        "waiting_rows": waiting_rows if show_waiting else [],
        "purchased_rows": purchased_rows if show_purchased else [],
        "production_batches": production_batches if show_production else [],
        "stock_list_type": list_type,
        "show_waiting": show_waiting,
        "show_purchased": show_purchased,
        "show_production": show_production,
        "q": q,
        "from_date": from_date_raw,
        "to_date": to_date_raw,
        "cost_status": cost_status,
        "material_type": material_type,
        "can_view_stock_cost": _can_view_stock_cost(request.user),
    })


@login_required
@permission_required("inventory.view_inventorybatch", raise_exception=True)
def inventory_fabric_print_labels(request, pk):
    """Print a Fabric purchase group using the existing Production 10x10cm label template."""
    from types import SimpleNamespace

    anchor = get_object_or_404(
        FabricReceipt.objects.select_related("color", "supplier_ref", "fabric_type"),
        pk=pk,
    )

    if anchor.purchase_group:
        receipts = list(
            FabricReceipt.objects.filter(purchase_group=anchor.purchase_group)
            .select_related("color", "supplier_ref", "fabric_type")
            .prefetch_related("rolls")
            .order_by("receipt_no", "id")
        )
    else:
        receipts = [anchor]

    labels = []

    for receipt in receipts:
        actual_rolls = list(receipt.rolls.all().order_by("id"))

        if actual_rolls:
            total = len(actual_rolls)
            for index, roll in enumerate(actual_rolls, start=1):
                labels.append({
                    "roll": roll,
                    "roll_no_text": index,
                    "roll_total_text": total,
                    "planned": False,
                })
            continue

        # Waiting purchase: create temporary in-memory roll objects only for label preview.
        # Nothing is written to the database here.
        weights = [
            Decimal(str(x))
            for x in (receipt.pending_roll_weights or [])
            if str(x).strip()
        ]

        total = int(receipt.roll_count or 0)
        while len(weights) < total:
            weights.append(Decimal("0"))

        color_code = (getattr(receipt.color, "code", "") or "COL").upper()

        for index in range(1, total + 1):
            planned_roll = SimpleNamespace(
                roll_code=f"{receipt.receipt_no}-{color_code}-{index:03d}",
                original_qty=weights[index - 1],
                receipt=receipt,
            )
            labels.append({
                "roll": planned_roll,
                "roll_no_text": index,
                "roll_total_text": total,
                "planned": True,
            })

    return render(request, "production/fabric_roll_labels.html", {
        "labels": labels,
        "back_receipt_id": None,
        "anchor": anchor,
        "group_ref": anchor.purchase_group or anchor.receipt_no,
    })


@login_required
@permission_required("inventory.change_inventorybatch", raise_exception=True)
@transaction.atomic
def inventory_fabric_confirm_received(request, pk):
    """Confirm a saved Fabric purchase. FabricRoll stock is created exactly once here."""
    anchor = get_object_or_404(FabricReceipt.objects.select_for_update(), pk=pk)
    if anchor.status == FabricReceipt.STATUS_RECEIVED:
        messages.info(request, "This fabric purchase is already received.")
        return redirect("inventory_stock_in_list")

    if anchor.purchase_group:
        receipts = list(
            FabricReceipt.objects.select_for_update()
            .filter(purchase_group=anchor.purchase_group, status=FabricReceipt.STATUS_WAITING)
            .select_related("fabric_type", "color", "supplier_ref")
            .order_by("id")
        )
    else:
        receipts = [anchor]

    receive_rows = []
    for receipt in receipts:
        expected = [Decimal(str(x)) for x in (receipt.pending_roll_weights or [])]
        while len(expected) < int(receipt.roll_count or 0):
            expected.append(Decimal("1"))
        expected = expected[: int(receipt.roll_count or 0)]
        receive_rows.append({"receipt": receipt, "weights": expected})

    if request.method == "POST":
        from datetime import date
        raw_date = (request.POST.get("receive_date") or "").strip()
        try:
            receive_date = date.fromisoformat(raw_date) if raw_date else timezone.localdate()
        except ValueError:
            messages.error(request, "Invalid received date.")
            return redirect("inventory_fabric_confirm_received", pk=anchor.pk)

        for item in receive_rows:
            receipt = item["receipt"]
            if receipt.rolls.exists():
                # Safety guard: never create duplicate physical rolls.
                receipt.status = FabricReceipt.STATUS_RECEIVED
                receipt.received_date = receive_date
                receipt.received_at = timezone.now()
                receipt.received_by = request.user
                receipt.updated_by = request.user
                receipt.pending_roll_weights = []
                receipt.save(update_fields=[
                    "status", "received_date", "received_at", "received_by",
                    "updated_by", "pending_roll_weights", "updated_at"
                ])
                continue

            actual_weights = []
            for index in range(1, int(receipt.roll_count or 0) + 1):
                raw = (request.POST.get(f"weight_{receipt.pk}_{index}") or "").strip()
                try:
                    weight = Decimal(raw)
                except Exception:
                    weight = Decimal("0")
                if weight <= 0:
                    messages.error(
                        request,
                        f"Enter a valid KG greater than 0 for {receipt.fabric_name} / {receipt.color.name} roll {index}."
                    )
                    return redirect("inventory_fabric_confirm_received", pk=anchor.pk)
                actual_weights.append(weight)

            create_fabric_rolls(receipt, actual_weights)
            receipt.status = FabricReceipt.STATUS_RECEIVED
            receipt.received_date = receive_date
            receipt.received_at = timezone.now()
            receipt.received_by = request.user
            receipt.updated_by = request.user
            receipt.pending_roll_weights = []
            receipt.save(update_fields=[
                "status", "received_date", "received_at", "received_by",
                "updated_by", "pending_roll_weights", "updated_at"
            ])

        messages.success(request, "Fabric received. Physical rolls were created and fabric stock increased once.")
        return redirect("inventory_stock_in_list")

    return render(request, "inventory/inventory_fabric_receive.html", {
        "anchor": anchor,
        "receive_rows": receive_rows,
        "today": timezone.localdate(),
    })


@login_required
@permission_required("inventory.change_inventorybatch", raise_exception=True)
@transaction.atomic
def inventory_batch_confirm_received(request, pk):
    """Receive all or part of a saved purchase without mixing arrival qty with available stock."""
    batch = get_object_or_404(
        InventoryBatch.objects.select_for_update().prefetch_related(
            "items__item", "items__color", "items__size"
        ),
        pk=pk,
        is_deleted=False,
    )

    if batch.status == InventoryBatch.STATUS_RECEIVED:
        messages.info(request, "This purchase is already fully received.")
        return redirect("inventory_stock_in_list")

    rows = list(batch.items.select_for_update().filter(is_active=True).select_related("item", "color", "size"))

    if request.method == "POST":
        receive_date_raw = (request.POST.get("receive_date") or "").strip()
        receive_date = timezone.localdate()
        if receive_date_raw:
            try:
                from datetime import date
                receive_date = date.fromisoformat(receive_date_raw)
            except ValueError:
                messages.error(request, "Invalid received date.")
                return redirect("inventory_batch_confirm_received", pk=batch.pk)

        received_any = False
        for row in rows:
            raw = (request.POST.get(f"receive_qty_{row.pk}") or "0").strip()
            try:
                receive_qty = Decimal(raw or "0")
            except Exception:
                messages.error(request, f"Invalid quantity for {row}.")
                return redirect("inventory_batch_confirm_received", pk=batch.pk)

            if receive_qty < 0:
                messages.error(request, "Receive quantity cannot be negative.")
                return redirect("inventory_batch_confirm_received", pk=batch.pk)

            pending = row.qty_pending_arrival
            if receive_qty > pending:
                messages.error(request, f"{row}: maximum remaining to receive is {pending}.")
                return redirect("inventory_batch_confirm_received", pk=batch.pk)

            if receive_qty == 0:
                continue

            before = Decimal(row.qty_remaining or 0)
            row.qty_arrived = Decimal(row.qty_arrived or 0) + receive_qty
            row.qty_remaining = before + receive_qty
            row.save(update_fields=["qty_arrived", "qty_remaining"])

            log_stock_in(
                batch_item=row,
                qty_before=before,
                qty_after=row.qty_remaining,
                batch=batch,
                user=request.user,
                remark=f"Purchase receipt +{receive_qty} from {batch.batch_no}",
            )
            received_any = True

        if not received_any:
            messages.info(request, "Enter at least one received quantity greater than 0.")
            return redirect("inventory_batch_confirm_received", pk=batch.pk)

        # Re-read locked rows after updates and decide whether purchase is complete.
        rows = list(batch.items.select_for_update().filter(is_active=True))
        fully_received = all(row.qty_pending_arrival <= 0 for row in rows)
        batch.status = InventoryBatch.STATUS_RECEIVED if fully_received else InventoryBatch.STATUS_PARTIAL
        batch.received_date = receive_date
        batch.received_at = timezone.now()
        batch.received_by = request.user
        batch.updated_by = request.user
        batch.save(update_fields=[
            "status", "received_date", "received_at", "received_by", "updated_by", "updated_at"
        ])

        _log_batch_history(
            batch,
            InventoryBatchHistory.ACTION_UPDATE,
            request.user,
            "Purchase fully received" if fully_received else "Purchase partially received",
        )
        try:
            _sync_inventory_batch_expense(batch, request.user)
        except Exception:
            logger.exception("Purchase receive succeeded, Finance sync failed for %s", batch.batch_no)

        if fully_received:
            messages.success(request, f"{batch.batch_no} fully received. All received stock has been added.")
        else:
            messages.success(request, f"{batch.batch_no} partially received. Only the quantities entered were added to stock.")
        return redirect("inventory_stock_in_list")

    return render(request, "inventory/inventory_batch_receive.html", {
        "batch": batch,
        "rows": rows,
        "today": timezone.localdate(),
    })

@login_required
@permission_required("inventory.add_inventorybatch", raise_exception=True)
@transaction.atomic
def inventory_batch_add_cost(request, pk):
    """Staff can add cost once; Finance viewers/admin can also edit an existing cost."""
    batch = get_object_or_404(InventoryBatch.objects.select_for_update(), pk=pk, is_deleted=False)
    can_view_cost = _can_view_stock_cost(request.user)

    # Purchase cost is private. Staff without cost permission must not be
    # able to see or change it, even by posting the URL directly.
    if not can_view_cost:
        messages.error(request, "You do not have permission to view or change purchase cost.")
        return redirect("inventory_stock_in_list")

    if request.method != "POST":
        return redirect("inventory_stock_in_list")
    if batch.cost_is_added and not can_view_cost:
        messages.error(request, "Cost is already added. Please ask Admin if it needs correction.")
        return redirect("inventory_stock_in_list")

    try:
        _apply_batch_cost_from_post(batch, request)
    except Exception:
        messages.error(request, "Invalid cost amount. Please enter numbers 0 or greater.")
        return redirect("inventory_stock_in_list")

    batch.updated_by = request.user
    batch.save(update_fields=[
        "total_goods_cost", "shipping_cost", "extra_cost", "cost_is_added",
        "cost_added_at", "cost_added_by", "updated_by", "updated_at"
    ])
    _sync_inventory_batch_expense(batch, request.user)
    _log_batch_history(batch, InventoryBatchHistory.ACTION_UPDATE, request.user, "Cost added/updated")
    messages.success(request, f"Cost saved for {batch.batch_no}.")
    return redirect("inventory_stock_in_list")


@login_required
@permission_required("inventory.change_inventorybatch", raise_exception=True)
@transaction.atomic
def inventory_batch_edit(request, pk):
    """
    Safely edit an existing InventoryBatch.

    Important:
    - Never hard-deletes the batch.
    - Keeps qty_remaining consistent when qty_received is edited.
    - Does not reset existing stock usage.
    - Keeps Finance Stock In expense synced.
    """
    batch = get_object_or_404(
        InventoryBatch.objects.select_for_update(),
        pk=pk,
        is_deleted=False,
    )

    existing_rows = {
        row.pk: row
        for row in batch.items.select_for_update().all()
    }

    if request.method == "POST":
        form = InventoryBatchForm(request.POST, instance=batch)
        formset = InventoryBatchItemFormSet(
            request.POST,
            instance=batch,
            prefix="items",
        )

        if form.is_valid() and formset.is_valid():
            before_snapshot = _batch_snapshot(batch)

            batch_obj = form.save(commit=False)
            batch_obj.updated_by = request.user
            batch_obj.save()

            # Validate deletions before applying any of them.
            for obj in formset.deleted_objects:
                old_row = existing_rows.get(obj.pk)
                if not old_row:
                    continue

                qty_received = Decimal(old_row.qty_received or 0)
                qty_remaining = Decimal(old_row.qty_remaining or 0)
                qty_used = qty_received - qty_remaining

                if qty_used != 0:
                    messages.error(
                        request,
                        "Cannot delete a stock row that already has stock used.",
                    )
                    transaction.set_rollback(True)
                    return redirect("inventory_batch_edit", pk=batch.pk)

            changed_items = formset.save(commit=False)

            # Apply deletions only after validation.
            for obj in formset.deleted_objects:
                obj.delete()

            for item in changed_items:
                if not item.item:
                    continue

                old_row = existing_rows.get(item.pk)

                if old_row:
                    old_received = Decimal(old_row.qty_received or 0)
                    old_remaining = Decimal(old_row.qty_remaining or 0)
                    used_qty = old_received - old_remaining
                    new_received = Decimal(item.qty_received or 0)

                    if new_received < used_qty:
                        messages.error(
                            request,
                            (
                                f"Cannot reduce {item.item} received quantity below "
                                f"the quantity already used ({used_qty})."
                            ),
                        )
                        transaction.set_rollback(True)
                        return redirect("inventory_batch_edit", pk=batch.pk)

                    # Preserve already-used stock and adjust only what remains.
                    item.qty_remaining = new_received - used_qty
                else:
                    # New row added while editing.
                    if batch.status == InventoryBatch.STATUS_RECEIVED:
                        item.qty_remaining = Decimal(item.qty_received or 0)
                    else:
                        item.qty_remaining = Decimal("0")

                item.batch = batch_obj
                item.base_unit_cost = Decimal(item.base_unit_cost or 0)
                item.final_unit_cost = Decimal(item.final_unit_cost or 0)
                item.is_active = True
                item.save()

            formset.save_m2m()

            _log_batch_history(
                batch_obj,
                InventoryBatchHistory.ACTION_UPDATE,
                request.user,
                "Batch updated",
            )

            try:
                _sync_inventory_batch_expense(batch_obj, request.user)
            except Exception:
                logger.exception(
                    "Inventory batch updated, but Finance expense sync failed for %s",
                    batch_obj.batch_no,
                )

            messages.success(
                request,
                f"Batch {batch_obj.batch_no} updated successfully.",
            )
            return redirect("inventory_batch_detail", pk=batch_obj.pk)

    else:
        form = InventoryBatchForm(instance=batch)
        formset = InventoryBatchItemFormSet(
            instance=batch,
            prefix="items",
        )

    return render(
        request,
        "inventory/inventory_batch_form.html",
        {
            "form": form,
            "formset": formset,
            "batch": batch,
            "stock_type": "printing"
            if batch.items.filter(item__item_type=InventoryItem.TYPE_FILM).exists()
            and not batch.items.filter(item__item_type=InventoryItem.TYPE_SHIRT).exists()
            else "cloth",
            "page_title": f"Edit Batch {batch.batch_no}",
            "submit_label": "Update Batch",
            "items": InventoryItem.objects.filter(is_active=True).order_by("code", "name"),
            "can_view_stock_cost": _can_view_stock_cost(request.user),
        },
    )


@login_required
@permission_required("inventory.delete_inventorybatch", raise_exception=True)
@transaction.atomic
def inventory_batch_delete(request, pk):
    """
    Soft-delete a stock-in batch.

    This does NOT remove database rows, so history/data is preserved.
    """
    batch = get_object_or_404(
        InventoryBatch.objects.select_for_update(),
        pk=pk,
        is_deleted=False,
    )

    if request.method == "POST":
        # Do not allow deleting a batch after any stock from it has been used.
        used_rows = []
        for row in batch.items.select_for_update().all():
            qty_received = Decimal(row.qty_received or 0)
            qty_remaining = Decimal(row.qty_remaining or 0)
            if qty_received - qty_remaining != 0:
                used_rows.append(row)

        if used_rows:
            messages.error(
                request,
                "Cannot delete this batch because some stock has already been used.",
            )
            return redirect("inventory_batch_detail", pk=batch.pk)

        batch.is_deleted = True
        batch.deleted_at = timezone.now()
        batch.deleted_by = request.user
        batch.updated_by = request.user
        batch.save(
            update_fields=[
                "is_deleted",
                "deleted_at",
                "deleted_by",
                "updated_by",
                "updated_at",
            ]
        )

        _log_batch_history(
            batch,
            InventoryBatchHistory.ACTION_DELETE,
            request.user,
            "Batch soft deleted",
        )

        messages.success(
            request,
            f"Batch {batch.batch_no} deleted safely.",
        )
        return redirect("inventory_stock_in_list")

    return render(
        request,
        "inventory/inventory_batch_delete.html",
        {"batch": batch},
    )


@login_required
@permission_required("inventory.view_inventorybatch", raise_exception=True)
def inventory_batch_detail(request, pk):
    batch = get_object_or_404(
        InventoryBatch.objects.select_related(
            "supplier_ref",
            "production_sewing_return__job__project",
            "production_sewing_return__job__project_color__color",
        ).prefetch_related(
            "items__item",
            "items__color",
            "items__size",
            "history_logs__changed_by",
        ),
        pk=pk,
    )
    is_production_stock_in = getattr(batch, "production_sewing_return", None) is not None
    return render(request, "inventory/inventory_batch_detail.html", {
        "batch": batch,
        "is_production_stock_in": is_production_stock_in,
    })


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

            old_qty = Decimal(batch_item.qty_remaining or 0)
            adjustment.qty_before = old_qty

            adjustment_type = form.cleaned_data["adjustment_type"]
            qty = form.cleaned_data.get("qty") or Decimal("0")
            stocktake_final_qty = form.cleaned_data.get("stocktake_final_qty")

            if adjustment_type == InventoryAdjustment.TYPE_STOCKTAKE:
                new_qty = Decimal(stocktake_final_qty or 0)
                diff = abs(new_qty - old_qty)
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

            log_adjustment(
                batch_item=batch_item,
                qty_before=old_qty,
                qty_after=new_qty,
                adjustment=adjustment,
                user=request.user,
                remark=adjustment.reason or f"Stock adjusted: {adjustment.adjustment_type}",
            )

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
    total_stock = Decimal("0")
    in_progress_qty = Decimal("0")
    available_after_production = Decimal("0")

    selected_item = None
    selected_color = None
    selected_size = None

    active_statuses = [
        Order.STATUS_PENDING,
        Order.STATUS_PROCESSING,
    ]

    if select_form.is_valid():
        selected_item = select_form.cleaned_data.get("item")
        selected_color = select_form.cleaned_data.get("color")
        selected_size = select_form.cleaned_data.get("size")

        qs = InventoryBatchItem.objects.select_related("batch", "item", "color", "size").filter(
            is_active=True,
            batch__is_deleted=False,
            item=selected_item,
        )

        if selected_color:
            qs = qs.filter(color=selected_color)

        if selected_size:
            qs = qs.filter(size=selected_size)

        selected_rows = list(qs.order_by("-batch__received_date", "-id"))
        total_stock = qs.aggregate(total=Sum("qty_remaining")).get("total") or Decimal("0")

        order_qs = OrderItem.objects.filter(
            shirt_item=selected_item,
            order__status__in=active_statuses,
            order__is_deleted=False,
        )

        if selected_color:
            order_qs = order_qs.filter(color=selected_color)

        if selected_size:
            order_qs = order_qs.filter(size=selected_size)

        for item in order_qs:
            remaining = Decimal(item.quantity or 0) - Decimal(item.done_qty or 0)
            if remaining > 0:
                in_progress_qty += remaining

        available_after_production = total_stock - in_progress_qty

        if request.method == "POST":
            adjust_form = InventoryAdjustVariantForm(request.POST)

            if adjust_form.is_valid():
                adjustment_type = adjust_form.cleaned_data["adjustment_type"]
                qty = adjust_form.cleaned_data.get("qty") or Decimal("0")
                final_qty = adjust_form.cleaned_data.get("final_qty")
                reason = adjust_form.cleaned_data.get("reason") or ""

                if adjustment_type == "STOCKTAKE":
                    final_qty = Decimal(final_qty or 0)
                    diff = final_qty - total_stock

                    if diff == 0:
                        messages.success(request, "No stock change needed.")
                        return redirect(request.path + "?" + request.META.get("QUERY_STRING", ""))

                    if diff > 0:
                        target = selected_rows[0] if selected_rows else None

                        if not target:
                            messages.error(request, "No stock row found to add into. Please create stock batch first.")
                            return redirect(request.path + "?" + request.META.get("QUERY_STRING", ""))

                        old_qty = Decimal(target.qty_remaining or 0)
                        target.qty_remaining = old_qty + diff
                        target.save(update_fields=["qty_remaining"])

                        adjustment = InventoryAdjustment.objects.create(
                            batch_item=target,
                            adjustment_type=InventoryAdjustment.TYPE_FOUND,
                            qty=diff,
                            reason=reason or f"Stock take adjusted total stock from {total_stock} to {final_qty}",
                            created_by=request.user if request.user.is_authenticated else None,
                            qty_before=old_qty,
                            qty_after=target.qty_remaining,
                        )

                        log_adjustment(
                            batch_item=target,
                            qty_before=old_qty,
                            qty_after=target.qty_remaining,
                            adjustment=adjustment,
                            user=request.user,
                            remark=adjustment.reason,
                        )

                    else:
                        remaining_to_reduce = abs(diff)

                        for row in selected_rows:
                            if remaining_to_reduce <= 0:
                                break

                            use_qty = min(Decimal(row.qty_remaining or 0), remaining_to_reduce)
                            old_qty = Decimal(row.qty_remaining or 0)
                            row.qty_remaining = old_qty - use_qty
                            row.save(update_fields=["qty_remaining"])

                            adjustment = InventoryAdjustment.objects.create(
                                batch_item=row,
                                adjustment_type=InventoryAdjustment.TYPE_STOCKTAKE,
                                qty=use_qty,
                                reason=reason or f"Stock take adjusted total stock from {total_stock} to {final_qty}",
                                created_by=request.user if request.user.is_authenticated else None,
                                qty_before=old_qty,
                                qty_after=row.qty_remaining,
                            )

                            log_adjustment(
                                batch_item=row,
                                qty_before=old_qty,
                                qty_after=row.qty_remaining,
                                adjustment=adjustment,
                                user=request.user,
                                remark=adjustment.reason,
                            )

                            remaining_to_reduce -= use_qty

                else:
                    if adjustment_type in ["ADD", "FOUND"]:
                        target = selected_rows[0] if selected_rows else None

                        if not target:
                            messages.error(request, "No stock row found to add into. Please create stock batch first.")
                            return redirect(request.path + "?" + request.META.get("QUERY_STRING", ""))

                        old_qty = Decimal(target.qty_remaining or 0)
                        target.qty_remaining = old_qty + qty
                        target.save(update_fields=["qty_remaining"])

                        adjustment = InventoryAdjustment.objects.create(
                            batch_item=target,
                            adjustment_type=InventoryAdjustment.TYPE_FOUND if adjustment_type == "FOUND" else InventoryAdjustment.TYPE_ADD,
                            qty=qty,
                            reason=reason,
                            created_by=request.user if request.user.is_authenticated else None,
                            qty_before=old_qty,
                            qty_after=target.qty_remaining,
                        )

                        log_adjustment(
                            batch_item=target,
                            qty_before=old_qty,
                            qty_after=target.qty_remaining,
                            adjustment=adjustment,
                            user=request.user,
                            remark=adjustment.reason or "Stock added",
                        )

                    else:
                        remaining_to_reduce = qty

                        if remaining_to_reduce > total_stock:
                            messages.error(request, "Cannot reduce more than total stock.")
                            return redirect(request.path + "?" + request.META.get("QUERY_STRING", ""))

                        type_map = {
                            "REMOVE": InventoryAdjustment.TYPE_REMOVE,
                            "LOST": InventoryAdjustment.TYPE_LOST,
                            "DAMAGE": InventoryAdjustment.TYPE_DAMAGE,
                        }

                        for row in selected_rows:
                            if remaining_to_reduce <= 0:
                                break

                            use_qty = min(Decimal(row.qty_remaining or 0), remaining_to_reduce)
                            old_qty = Decimal(row.qty_remaining or 0)
                            row.qty_remaining = old_qty - use_qty
                            row.save(update_fields=["qty_remaining"])

                            adjustment = InventoryAdjustment.objects.create(
                                batch_item=row,
                                adjustment_type=type_map[adjustment_type],
                                qty=use_qty,
                                reason=reason,
                                created_by=request.user if request.user.is_authenticated else None,
                                qty_before=old_qty,
                                qty_after=row.qty_remaining,
                            )

                            log_adjustment(
                                batch_item=row,
                                qty_before=old_qty,
                                qty_after=row.qty_remaining,
                                adjustment=adjustment,
                                user=request.user,
                                remark=adjustment.reason or "Stock reduced",
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
            "total_stock": _to_int(total_stock),
            "in_progress_qty": _to_int(in_progress_qty),
            "available_after_production": _to_int(available_after_production),
            "selected_item": selected_item,
            "selected_color": selected_color,
            "selected_size": selected_size,
        },
    )


@login_required
@permission_required("inventory.add_inventoryadjustment", raise_exception=True)
@transaction.atomic
def material_usage(request):
    material_types = [
        InventoryItem.TYPE_FILM,
        InventoryItem.TYPE_INK,
        InventoryItem.TYPE_POWDER,
        InventoryItem.TYPE_MAINTENANCE,
        InventoryItem.TYPE_OTHER,
    ]

    materials = InventoryItem.objects.filter(
        is_active=True,
        item_type__in=material_types,
    ).order_by("item_type", "code", "name")

    material_rows = []

    for item in materials:
        stock = (
            InventoryBatchItem.objects.filter(
                item=item,
                is_active=True,
                batch__is_deleted=False,
            )
            .aggregate(total=Sum("qty_remaining"))
            .get("total")
            or Decimal("0")
        )

        if item.item_type == InventoryItem.TYPE_FILM:
            quick_qty = Decimal("1")
            quick_label = "Use 1 Roll"
        elif item.item_type == InventoryItem.TYPE_INK:
            quick_qty = Decimal("1")
            quick_label = "Use 1 Bottle"
        elif item.item_type == InventoryItem.TYPE_POWDER:
            quick_qty = Decimal("1")
            quick_label = "Use 1 Pack"
        elif item.item_type == InventoryItem.TYPE_MAINTENANCE:
            quick_qty = Decimal("1")
            quick_label = "Use 1 PCS"
        else:
            quick_qty = Decimal("1")
            quick_label = "Use 1"

        material_rows.append(
            {
                "item": item,
                "stock": stock,
                "quick_qty": quick_qty,
                "quick_label": quick_label,
            }
        )

    if request.method == "POST":
        item_id = request.POST.get("item_id")
        qty = Decimal(str(request.POST.get("qty") or "0"))
        reason = (request.POST.get("reason") or "").strip()

        item = get_object_or_404(
            InventoryItem,
            pk=item_id,
            item_type__in=material_types,
            is_active=True,
        )

        if qty <= 0:
            messages.error(request, "Qty must be greater than 0.")
            return redirect("material_usage")

        stock_rows = list(
            InventoryBatchItem.objects.select_related("batch", "item")
            .filter(
                item=item,
                is_active=True,
                batch__is_deleted=False,
                qty_remaining__gt=0,
            )
            .order_by("-batch__received_date", "-id")
        )

        total_stock = sum((row.qty_remaining or Decimal("0")) for row in stock_rows)

        if qty > total_stock:
            messages.error(request, f"Not enough stock. Current stock: {total_stock}")
            return redirect("material_usage")

        remaining_to_reduce = qty

        for row in stock_rows:
            if remaining_to_reduce <= 0:
                break

            use_qty = min(Decimal(row.qty_remaining or 0), remaining_to_reduce)
            old_qty = Decimal(row.qty_remaining or 0)
            row.qty_remaining = old_qty - use_qty
            row.save(update_fields=["qty_remaining"])

            adjustment = InventoryAdjustment.objects.create(
                batch_item=row,
                adjustment_type=InventoryAdjustment.TYPE_REMOVE,
                qty=use_qty,
                reason=reason or "Material usage",
                created_by=request.user if request.user.is_authenticated else None,
                qty_before=old_qty,
                qty_after=row.qty_remaining,
            )

            log_adjustment(
                batch_item=row,
                qty_before=old_qty,
                qty_after=row.qty_remaining,
                adjustment=adjustment,
                user=request.user,
                remark=adjustment.reason or "Material usage",
            )

            remaining_to_reduce -= use_qty

        messages.success(request, f"{item.name} deducted successfully.")
        return redirect("material_usage")

    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()
    item_id = (request.GET.get("item") or "").strip()
    user_id = (request.GET.get("user") or "").strip()

    usage_qs = (
        InventoryAdjustment.objects.select_related(
            "batch_item",
            "batch_item__item",
            "created_by",
        )
        .filter(
            adjustment_type=InventoryAdjustment.TYPE_REMOVE,
            batch_item__item__item_type__in=material_types,
            batch_item__batch__is_deleted=False,
        )
        .order_by("-created_at", "-id")
    )

    if date_from:
        usage_qs = usage_qs.filter(created_at__date__gte=date_from)

    if date_to:
        usage_qs = usage_qs.filter(created_at__date__lte=date_to)

    if item_id:
        usage_qs = usage_qs.filter(batch_item__item_id=item_id)

    if user_id:
        usage_qs = usage_qs.filter(created_by_id=user_id)

    users = (
        InventoryAdjustment.objects.filter(
            adjustment_type=InventoryAdjustment.TYPE_REMOVE,
            batch_item__item__item_type__in=material_types,
            created_by__isnull=False,
        )
        .select_related("created_by")
        .order_by("created_by__username")
        .values("created_by_id", "created_by__username")
        .distinct()
    )

    total_used = usage_qs.aggregate(total=Sum("qty")).get("total") or Decimal("0")

    return render(
        request,
        "inventory/material_usage.html",
        {
            "material_rows": material_rows,
            "usage_rows": usage_qs[:300],
            "materials": materials,
            "users": users,
            "date_from": date_from,
            "date_to": date_to,
            "selected_item_id": item_id,
            "selected_user_id": user_id,
            "total_used": total_used,
        },
    )


@login_required
@permission_required("inventory.view_stockledger", raise_exception=True)
def stock_ledger_list(request):
    keyword = (request.GET.get("q") or "").strip()
    movement_type = (request.GET.get("movement_type") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()
    only_correct_forward = request.GET.get("from_correct") == "1"

    item_id = (request.GET.get("item") or "").strip()
    color_id = (request.GET.get("color") or "").strip()
    size_id = (request.GET.get("size") or "").strip()

    qs = (
        StockLedger.objects
        .select_related(
            "batch_item",
            "batch_item__batch",
            "batch_item__item",
            "batch_item__color",
            "batch_item__size",
            "created_by",
        )
        .order_by("-created_at", "-id")
    )

    if keyword:
        qs = qs.filter(
            Q(reference_no__icontains=keyword)
            | Q(order_no__icontains=keyword)
            | Q(batch_no__icontains=keyword)
            | Q(remark__icontains=keyword)
            | Q(correct_remark__icontains=keyword)
            | Q(batch_item__item__name__icontains=keyword)
            | Q(batch_item__item__code__icontains=keyword)
            | Q(batch_item__color__name__icontains=keyword)
            | Q(batch_item__size__name__icontains=keyword)
        )

    if movement_type:
        qs = qs.filter(movement_type=movement_type)

    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)

    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    if item_id:
        qs = qs.filter(batch_item__item_id=item_id)

    if color_id:
        qs = qs.filter(batch_item__color_id=color_id)

    if size_id:
        qs = qs.filter(batch_item__size_id=size_id)

    last_correct = None
    if only_correct_forward and item_id:
        correct_qs = StockLedger.objects.filter(
            is_correct_checkpoint=True,
            batch_item__item_id=item_id,
        )

        if color_id:
            correct_qs = correct_qs.filter(batch_item__color_id=color_id)

        if size_id:
            correct_qs = correct_qs.filter(batch_item__size_id=size_id)

        last_correct = correct_qs.order_by("-created_at", "-id").first()

        if last_correct:
            qs = qs.filter(created_at__gte=last_correct.created_at)

    return render(
        request,
        "inventory/stock_ledger_list.html",
        {
            "rows": qs[:500],
            "keyword": keyword,
            "movement_type": movement_type,
            "date_from": date_from,
            "date_to": date_to,
            "only_correct_forward": only_correct_forward,
            "last_correct": last_correct,
            "selected_item_id": item_id,
            "selected_color_id": color_id,
            "selected_size_id": size_id,
            "items": InventoryItem.objects.filter(is_active=True).order_by("item_type", "code", "name"),
            "colors": Color.objects.filter(is_active=True).order_by("name"),
            "sizes": Size.objects.filter(is_active=True).order_by("sort_order", "id"),
            "movement_choices": StockLedger.TYPE_CHOICES,
        },
    )


@login_required
@permission_required("inventory.view_stockledger", raise_exception=True)
def stock_ledger_by_batch_item(request, batch_item_id):
    batch_item = get_object_or_404(
        InventoryBatchItem.objects.select_related("batch", "item", "color", "size"),
        pk=batch_item_id,
    )

    only_correct_forward = request.GET.get("from_correct") == "1"

    qs = (
        StockLedger.objects
        .select_related("created_by", "batch_item", "batch_item__item", "batch_item__color", "batch_item__size")
        .filter(batch_item=batch_item)
        .order_by("-created_at", "-id")
    )

    last_correct = (
        StockLedger.objects
        .filter(batch_item=batch_item, is_correct_checkpoint=True)
        .order_by("-created_at", "-id")
        .first()
    )

    if only_correct_forward and last_correct:
        qs = qs.filter(created_at__gte=last_correct.created_at)

    return render(
        request,
        "inventory/stock_ledger_list.html",
        {
            "rows": qs[:500],
            "batch_item": batch_item,
            "only_correct_forward": only_correct_forward,
            "last_correct": last_correct,
            "items": InventoryItem.objects.filter(is_active=True).order_by("item_type", "code", "name"),
            "colors": Color.objects.filter(is_active=True).order_by("name"),
            "sizes": Size.objects.filter(is_active=True).order_by("sort_order", "id"),
            "movement_choices": StockLedger.TYPE_CHOICES,
        },
    )


@login_required
@permission_required("inventory.add_stockledger", raise_exception=True)
@transaction.atomic
def correct_stock_count_view(request, batch_item_id):
    batch_item = get_object_or_404(
        InventoryBatchItem.objects.select_related("batch", "item", "color", "size"),
        pk=batch_item_id,
        batch__is_deleted=False,
    )

    if request.method == "POST":
        correct_qty = Decimal(str(request.POST.get("correct_qty") or "0"))
        remark = (request.POST.get("remark") or "").strip()

        if correct_qty < 0:
            messages.error(request, "Correct qty cannot be below 0.")
            return redirect("correct_stock_count", batch_item_id=batch_item.pk)

        correct_stock_count(
            batch_item=batch_item,
            correct_qty=correct_qty,
            user=request.user,
            remark=remark or "Correct stock count. Track from this date forward.",
        )

        _log_batch_history(
            batch_item.batch,
            InventoryBatchHistory.ACTION_UPDATE,
            request.user,
            f"Correct stock count for row {batch_item.id}: {correct_qty}",
        )

        messages.success(request, "Stock marked as correct. Next time check from this date forward.")
        return redirect("stock_ledger_by_batch_item", batch_item_id=batch_item.pk)

    return render(
        request,
        "inventory/correct_stock_form.html",
        {
            "batch_item": batch_item,
        },
    )