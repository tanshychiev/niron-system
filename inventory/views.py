import logging
from collections import OrderedDict, defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.db.models import Q, Sum
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
    if not receipts:
        return None
    refs = [receipt.receipt_no for receipt in receipts if receipt.receipt_no]
    reference = refs[0] if len(refs) == 1 else f"{refs[0]} +{len(refs)-1}"
    return Expense.objects.create(
        expense_type=Expense.TYPE_BATCH,
        created_by=user,
        amount=Decimal("0.00"),
        batch_cost=Decimal("0.00"),
        batch_delivery_fee=Decimal("0.00"),
        batch_other_fee=Decimal("0.00"),
        expense_status=Expense.STATUS_PENDING,
        stock_source_type=Expense.SOURCE_FABRIC,
        source_reference=reference,
        supplier_name=supplier.name if supplier else "",
        received_date=received_date,
        fabric_receipt_ids=[receipt.pk for receipt in receipts],
        note="Auto-created from Fabric Stock In. Cost pending.",
    )


@login_required
@permission_required("inventory.add_inventorybatch", raise_exception=True)
@transaction.atomic
def inventory_batch_create(request):
    stock_type = (request.POST.get("stock_type") or request.GET.get("type") or "cloth").strip().lower()
    if stock_type not in {"cloth", "printing", "fabric"}:
        stock_type = "cloth"

    # ---------------------------------------------------------
    # FABRIC: save through the existing Production receipt/roll
    # models so no duplicate fabric stock system is created.
    # ---------------------------------------------------------
    if stock_type == "fabric":
        if request.method == "POST":
            fabric_header_form = FabricReceiptHeaderForm(request.POST)
            fabric_formset = fabric_receipt_line_formset(
                data=request.POST,
                user=request.user,
                prefix="fabric_items",
            )

            if fabric_header_form.is_valid() and fabric_formset.is_valid():
                total_rolls = 0
                saved_lines = 0
                created_receipts = []

                for line_form in fabric_formset:
                    if not line_form.cleaned_data or line_form.cleaned_data.get("DELETE"):
                        continue

                    data = line_form.cleaned_data
                    supplier = fabric_header_form.cleaned_data["supplier"]

                    fabric_type = data["fabric_type"]
                    receipt = FabricReceipt(
                        received_date=fabric_header_form.cleaned_data["received_date"],
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
                    create_fabric_rolls(receipt)

                    created_receipts.append(receipt)
                    total_rolls += receipt.roll_count
                    saved_lines += 1

                finance_warning = ""
                try:
                    _create_pending_fabric_expense(
                        created_receipts,
                        fabric_header_form.cleaned_data["supplier"],
                        fabric_header_form.cleaned_data["received_date"],
                        request.user,
                    )
                except Exception:
                    logger.exception(
                        "Fabric Stock In saved, but Finance expense sync failed."
                    )
                    finance_warning = (
                        " Stock was saved, but Finance expense sync needs review."
                    )

                message = (
                    f"{total_rolls} fabric roll(s) across "
                    f"{saved_lines} fabric type(s) received successfully."
                    f"{finance_warning}"
                )

                if request.headers.get("x-requested-with") == "XMLHttpRequest":
                    return JsonResponse(
                        {
                            "ok": True,
                            "message": message,
                            "detail_url": (
                                f"/production/fabric-receipts/{created_receipts[0].pk}/edit/"
                                if created_receipts
                                else "/inventory/?type=fabric"
                            ),
                        }
                    )

                messages.success(request, message)
                return redirect("/inventory/?type=fabric")

            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                transaction.set_rollback(True)

                field_errors = {}
                for name, errors in fabric_header_form.errors.items():
                    field_errors[f"id_{name}"] = [str(error) for error in errors]

                row_errors = {}
                for index, row_form in enumerate(fabric_formset.forms):
                    for name, errors in row_form.errors.items():
                        row_errors[f"id_fabric_items-{index}-{name}"] = [
                            str(error) for error in errors
                        ]

                first = (
                    next(iter(field_errors.values()), None)
                    or next(iter(row_errors.values()), None)
                )
                message = first[0] if first else "Please check the highlighted fields."

                return JsonResponse(
                    {
                        "ok": False,
                        "message": f"Failed: {message}",
                        "field_errors": field_errors,
                        "row_errors": row_errors,
                    },
                    status=400,
                )
        else:
            fabric_header_form = FabricReceiptHeaderForm(
                initial={"received_date": timezone.localdate()}
            )
            fabric_formset = fabric_receipt_line_formset(
                user=request.user,
                prefix="fabric_items",
            )

        # Keep the ordinary forms available because the template contains both modes.
        form = InventoryBatchForm(initial={"received_date": timezone.localdate()})
        formset = InventoryBatchItemFormSet(prefix="items")

        return render(
            request,
            "inventory/inventory_batch_form.html",
            {
                "form": form,
                "formset": formset,
                "fabric_header_form": fabric_header_form,
                "fabric_formset": fabric_formset,
                "stock_type": "fabric",
                "page_title": "Stock In",
                "submit_label": "Save Fabric Batch",
                "items": InventoryItem.objects.filter(is_active=True).order_by("code", "name"),
            },
        )

    # ---------------------------------------------------------
    # CLOTH / PRINTING MATERIAL: existing inventory batch logic.
    # ---------------------------------------------------------
    if request.method == "POST":
        form = InventoryBatchForm(request.POST)
        formset = InventoryBatchItemFormSet(request.POST, prefix="items")

        if form.is_valid() and formset.is_valid():
            batch = form.save(commit=False)
            # Warehouse staff do not enter or see purchase cost. Finance fills it later.
            batch.total_goods_cost = Decimal("0.00")
            batch.shipping_cost = Decimal("0.00")
            batch.extra_cost = Decimal("0.00")

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

                if not item.qty_remaining:
                    item.qty_remaining = item.qty_received

                item.save()

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
                "Batch created",
            )
            finance_warning = ""
            try:
                pending_expense = _create_pending_inventory_expense(
                    batch,
                    request.user,
                )
                if pending_expense:
                    pending_expense.note = (
                        f"Auto-created from {stock_type.title()} Stock In. "
                        "Cost pending."
                    )
                    pending_expense.save(update_fields=["note"])
            except Exception:
                logger.exception(
                    "%s Stock In saved, but Finance expense sync failed.",
                    stock_type,
                )
                finance_warning = (
                    " Stock was saved, but Finance expense sync needs review."
                )

            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse(
                    {
                        "ok": True,
                        "message": (
                            f"Inventory batch {batch.batch_no} created successfully."
                            f"{finance_warning}"
                        ),
                        "detail_url": f"/inventory/batches/{batch.pk}/",
                    }
                )

            messages.success(request, f"Inventory batch {batch.batch_no} created.")
            return redirect("inventory_batch_detail", pk=batch.pk)

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            transaction.set_rollback(True)
            return JsonResponse(_form_error_payload(form, formset), status=400)
    else:
        form = InventoryBatchForm(initial={"received_date": timezone.localdate()})
        formset = InventoryBatchItemFormSet(prefix="items")

    fabric_header_form = FabricReceiptHeaderForm(
        initial={"received_date": timezone.localdate()}
    )
    fabric_formset = fabric_receipt_line_formset(
        user=request.user,
        prefix="fabric_items",
    )

    return render(
        request,
        "inventory/inventory_batch_form.html",
        {
            "form": form,
            "formset": formset,
            "fabric_header_form": fabric_header_form,
            "fabric_formset": fabric_formset,
            "stock_type": stock_type,
            "page_title": "Stock In",
            "submit_label": "Save Batch",
            "items": InventoryItem.objects.filter(is_active=True).order_by("code", "name"),
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

                old_qty = Decimal(obj.qty_remaining or 0)

                log_batch_delete(
                    batch_item=obj,
                    qty_before=old_qty,
                    qty_after=Decimal("0"),
                    batch=batch,
                    user=request.user,
                    remark=f"Batch row removed from active stock in {batch.batch_no}",
                )

                obj.qty_remaining = Decimal("0")
                obj.is_active = False
                obj.save(update_fields=["qty_remaining", "is_active"])

            for item in items:
                if not item.item:
                    continue

                is_new = item.pk is None
                old_qty = Decimal("0")

                if not is_new:
                    old_obj = InventoryBatchItem.objects.get(pk=item.pk)
                    old_qty = Decimal(old_obj.qty_remaining or 0)

                item.batch = batch
                item.base_unit_cost = 0
                item.final_unit_cost = 0
                item.is_active = True
                item.save()

                new_qty = Decimal(item.qty_remaining or 0)

                if is_new:
                    log_stock_in(
                        batch_item=item,
                        qty_before=Decimal("0"),
                        qty_after=new_qty,
                        batch=batch,
                        user=request.user,
                        remark=f"New stock row added in batch {batch.batch_no}",
                    )
                elif new_qty != old_qty:
                    log_batch_edit(
                        batch_item=item,
                        qty_before=old_qty,
                        qty_after=new_qty,
                        batch=batch,
                        user=request.user,
                        remark=f"Batch edited: {batch.batch_no}",
                    )

            _log_batch_history(batch, InventoryBatchHistory.ACTION_UPDATE, request.user, "Batch updated")
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"ok": True, "message": f"Batch {batch.batch_no} updated successfully.", "redirect_url": f"/inventory/batches/{batch.pk}/"})
            messages.success(request, f"Batch {batch.batch_no} updated.")
            return redirect("inventory_batch_detail", pk=batch.pk)
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            transaction.set_rollback(True)
            return JsonResponse(_form_error_payload(form, formset), status=400)
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
    batch = get_object_or_404(
        InventoryBatch.objects.prefetch_related("items"),
        pk=pk,
        is_deleted=False,
    )

    if request.method == "POST":
        for row in batch.items.filter(is_active=True):
            old_qty = Decimal(row.qty_remaining or 0)

            log_batch_delete(
                batch_item=row,
                qty_before=old_qty,
                qty_after=Decimal("0"),
                batch=batch,
                user=request.user,
                remark=f"Batch soft deleted: {batch.batch_no}",
            )

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
        InventoryBatch.objects.select_related("supplier_ref").prefetch_related(
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