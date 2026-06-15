from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render
from django.utils import timezone

from .models import InventoryBatchItem, InventoryItem, StockLedger


SHIRT_SIZE_ORDER = {
    "S": 1,
    "M": 2,
    "L": 3,
    "XL": 4,
    "XXL": 5,
    "XXXL": 6,
}


def _size_sort_value(size_name, fallback=9999):
    normalized = (size_name or "").strip().upper()
    return SHIRT_SIZE_ORDER.get(normalized, 100 + int(fallback or 9999))


def _variant_key(item_id, color_id=None, size_id=None, is_material=False):
    if is_material:
        return f"M:{item_id}"
    return f"S:{item_id}:{color_id or 0}:{size_id or 0}"


def _stock_queryset(item_id, color_id=None, size_id=None, is_material=False):
    qs = (
        InventoryBatchItem.objects
        .select_related("batch", "item", "color", "size")
        .filter(
            is_active=True,
            batch__is_deleted=False,
            item_id=item_id,
        )
        .order_by("batch__received_date", "id")
    )

    if is_material:
        return qs.exclude(item__item_type=InventoryItem.TYPE_SHIRT)

    qs = qs.filter(item__item_type=InventoryItem.TYPE_SHIRT)

    if color_id:
        qs = qs.filter(color_id=color_id)
    else:
        qs = qs.filter(color__isnull=True)

    if size_id:
        qs = qs.filter(size_id=size_id)
    else:
        qs = qs.filter(size__isnull=True)

    return qs


def _safe_image_url(item):
    try:
        return item.image.url if item.image else ""
    except (ValueError, AttributeError):
        return ""


def _collect_stock_data():
    rows = (
        InventoryBatchItem.objects
        .select_related("batch", "item", "color", "size")
        .filter(
            is_active=True,
            batch__is_deleted=False,
            item__is_active=True,
        )
        .order_by(
            "item__item_type",
            "item__sample_style",
            "item__code",
            "item__name",
            "color__name",
            "size__sort_order",
            "size__id",
            "batch__received_date",
            "id",
        )
    )

    variants = {}

    for row in rows:
        is_material = row.item.item_type != InventoryItem.TYPE_SHIRT
        key = _variant_key(
            row.item_id,
            row.color_id,
            row.size_id,
            is_material=is_material,
        )

        if key not in variants:
            size_name = row.size.name if row.size else "-"

            variants[key] = {
                "key": key,
                "is_material": is_material,
                "item_id": row.item_id,
                "item_code": row.item.code,
                "item_name": row.item.name,
                "item_type": row.item.item_type,
                "item_type_label": row.item.get_item_type_display(),
                "unit": row.item.get_unit_display(),
                "image_url": _safe_image_url(row.item),
                "style": row.item.sample_style,
                "style_label": (
                    row.item.get_sample_style_display()
                    if not is_material
                    else row.item.get_item_type_display()
                ),
                "color_id": 0 if is_material else (row.color_id or 0),
                "color_name": "-" if is_material else (row.color.name if row.color else "-"),
                "color_hex": (
                    "#E5E7EB"
                    if is_material
                    else (row.color.hex_code if row.color else "#D1D5DB")
                ),
                "size_id": 0 if is_material else (row.size_id or 0),
                "size_name": "All" if is_material else size_name,
                "size_sort": (
                    0
                    if is_material
                    else _size_sort_value(
                        size_name,
                        row.size.sort_order if row.size else 9999,
                    )
                ),
                "current_qty": Decimal("0"),
                "last_confirmed_at": None,
                "last_confirmed_by": None,
                "last_confirmed_qty": None,
                "input_step": "0.01" if is_material else "1",
            }

        variants[key]["current_qty"] += Decimal(row.qty_remaining or 0)

    confirm_logs = (
        StockLedger.objects
        .select_related(
            "created_by",
            "batch_item__item",
            "batch_item__color",
            "batch_item__size",
        )
        .filter(
            movement_type=StockLedger.TYPE_CORRECT,
            is_correct_checkpoint=True,
        )
        .order_by("-created_at", "-id")
    )

    seen = set()

    for log in confirm_logs:
        item = log.batch_item.item
        is_material = item.item_type != InventoryItem.TYPE_SHIRT
        key = _variant_key(
            item.id,
            log.batch_item.color_id,
            log.batch_item.size_id,
            is_material=is_material,
        )

        if key in variants and key not in seen:
            variants[key]["last_confirmed_at"] = log.created_at
            variants[key]["last_confirmed_by"] = log.created_by
            variants[key]["last_confirmed_qty"] = log.qty_after
            seen.add(key)

    shirt_grouped = defaultdict(list)
    material_rows = []

    for variant in variants.values():
        if variant["is_material"]:
            material_rows.append(variant)
            continue

        group_key = (
            variant["style"],
            variant["item_id"],
            variant["color_id"],
        )
        shirt_grouped[group_key].append(variant)

    style_order = {
        InventoryItem.STYLE_OVERSIZE: 1,
        InventoryItem.STYLE_POLO: 2,
        InventoryItem.STYLE_BOXY: 3,
    }

    shirt_groups = []

    for (style, item_id, color_id), variant_rows in shirt_grouped.items():
        variant_rows.sort(
            key=lambda value: (
                value["size_sort"],
                value["size_name"],
            )
        )

        first = variant_rows[0]

        shirt_groups.append(
            {
                "style": style,
                "style_label": first["style_label"],
                "style_sort": style_order.get(style, 999),
                "item_id": item_id,
                "item_code": first["item_code"],
                "item_name": first["item_name"],
                "image_url": first["image_url"],
                "color_id": color_id,
                "color_name": first["color_name"],
                "color_hex": first["color_hex"],
                "rows": variant_rows,
            }
        )

    shirt_groups.sort(
        key=lambda group: (
            group["style_sort"],
            group["item_code"],
            group["item_name"],
            group["color_name"],
        )
    )

    material_rows.sort(
        key=lambda value: (
            value["item_type_label"],
            value["item_code"],
            value["item_name"],
        )
    )

    return shirt_groups, material_rows, variants


def _confirm_variant(
    *,
    item_id,
    color_id,
    size_id,
    is_material,
    real_qty,
    user,
    note="",
):
    real_qty = Decimal(real_qty)

    rows = list(
        _stock_queryset(
            item_id=item_id,
            color_id=color_id,
            size_id=size_id,
            is_material=is_material,
        )
    )

    if not rows:
        raise ValueError("Stock row no longer exists.")

    current_total = sum(
        (Decimal(row.qty_remaining or 0) for row in rows),
        Decimal("0"),
    )

    difference = real_qty - current_total
    anchor = rows[-1]

    if difference > 0:
        anchor.qty_remaining = Decimal(anchor.qty_remaining or 0) + difference
        anchor.save(update_fields=["qty_remaining"])

    elif difference < 0:
        amount_to_remove = abs(difference)

        for row in reversed(rows):
            if amount_to_remove <= 0:
                break

            row_qty = Decimal(row.qty_remaining or 0)

            if row_qty <= 0:
                continue

            remove_qty = min(row_qty, amount_to_remove)
            row.qty_remaining = row_qty - remove_qty
            row.save(update_fields=["qty_remaining"])
            amount_to_remove -= remove_qty

        # This project allows negative stock. If the requested final quantity
        # is below zero, keep the remaining shortage on the newest row.
        if amount_to_remove > 0:
            anchor.refresh_from_db(fields=["qty_remaining"])
            anchor.qty_remaining = Decimal(anchor.qty_remaining or 0) - amount_to_remove
            anchor.save(update_fields=["qty_remaining"])

    final_note = (note or "").strip() or "Stock checked and confirmed correct."
    now = timezone.now()

    reference = (
        f"CONFIRM-{now:%Y%m%d%H%M%S}-"
        f"{item_id}-{color_id or 0}-{size_id or 0}"
    )

    diff = real_qty - current_total

    StockLedger.objects.create(
        batch_item=anchor,
        movement_type=StockLedger.TYPE_CORRECT,
        qty_before=current_total,
        qty_in=diff if diff > 0 else Decimal("0"),
        qty_out=abs(diff) if diff < 0 else Decimal("0"),
        qty_after=real_qty,
        reference_no=reference,
        source_type=StockLedger.SOURCE_CORRECT,
        source_id=anchor.id,
        batch_no=anchor.batch.batch_no,
        remark=final_note,
        is_correct_checkpoint=True,
        correct_remark=final_note,
        created_by=user if user and user.is_authenticated else None,
        created_at=now,
    )

    return current_total, real_qty


@login_required
@permission_required("inventory.add_stockledger", raise_exception=True)
@transaction.atomic
def stock_confirm(request):
    shirt_groups, material_rows, variants = _collect_stock_data()

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        try:
            if action == "confirm_all":
                confirmed = 0

                for variant in variants.values():
                    _confirm_variant(
                        item_id=variant["item_id"],
                        color_id=variant["color_id"],
                        size_id=variant["size_id"],
                        is_material=variant["is_material"],
                        real_qty=variant["current_qty"],
                        user=request.user,
                        note="All listed stock checked and confirmed correct.",
                    )
                    confirmed += 1

                messages.success(
                    request,
                    f"{confirmed} stock rows confirmed correct. Date and staff were recorded.",
                )
                return redirect("stock_confirm")

            key = (request.POST.get("key") or "").strip()
            variant = variants.get(key)

            if not variant:
                messages.error(request, "The selected stock row was not found.")
                return redirect("stock_confirm")

            note = (request.POST.get("note") or "").strip()

            if action == "confirm_correct":
                real_qty = variant["current_qty"]

            elif action == "update_confirm":
                raw_qty = (request.POST.get("real_qty") or "").strip()

                try:
                    real_qty = Decimal(raw_qty)
                except (InvalidOperation, TypeError):
                    messages.error(request, "Enter a valid real quantity.")
                    return redirect("stock_confirm")

            else:
                messages.error(request, "Invalid stock confirmation action.")
                return redirect("stock_confirm")

            before, after = _confirm_variant(
                item_id=variant["item_id"],
                color_id=variant["color_id"],
                size_id=variant["size_id"],
                is_material=variant["is_material"],
                real_qty=real_qty,
                user=request.user,
                note=note,
            )

            label_parts = [variant["item_name"]]

            if not variant["is_material"]:
                label_parts.extend(
                    [
                        variant["color_name"],
                        variant["size_name"],
                    ]
                )

            label = " / ".join(label_parts)

            if before == after:
                messages.success(request, f"{label} confirmed correct.")
            else:
                messages.success(
                    request,
                    f"{label} updated from {before:g} to {after:g} and confirmed.",
                )

        except (ValueError, InvalidOperation) as exc:
            messages.error(request, str(exc))

        return redirect("stock_confirm")

    return render(
        request,
        "inventory/stock_confirm.html",
        {
            "shirt_groups": shirt_groups,
            "material_rows": material_rows,
            "variant_count": len(variants),
            "shirt_count": sum(len(group["rows"]) for group in shirt_groups),
            "material_count": len(material_rows),
        },
    )


@login_required
@permission_required("inventory.view_stockledger", raise_exception=True)
def stock_history(request):
    keyword = (request.GET.get("q") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    rows = (
        StockLedger.objects
        .select_related(
            "created_by",
            "batch_item__item",
            "batch_item__color",
            "batch_item__size",
            "batch_item__batch",
        )
        .filter(
            movement_type=StockLedger.TYPE_CORRECT,
            is_correct_checkpoint=True,
        )
        .order_by("-created_at", "-id")
    )

    if keyword:
        rows = rows.filter(
            Q(batch_item__item__name__icontains=keyword)
            | Q(batch_item__item__code__icontains=keyword)
            | Q(batch_item__color__name__icontains=keyword)
            | Q(batch_item__size__name__icontains=keyword)
            | Q(created_by__username__icontains=keyword)
            | Q(created_by__first_name__icontains=keyword)
            | Q(created_by__last_name__icontains=keyword)
            | Q(remark__icontains=keyword)
        )

    if date_from:
        rows = rows.filter(created_at__date__gte=date_from)

    if date_to:
        rows = rows.filter(created_at__date__lte=date_to)

    return render(
        request,
        "inventory/stock_history.html",
        {
            "rows": rows[:500],
            "keyword": keyword,
            "date_from": date_from,
            "date_to": date_to,
        },
    )
