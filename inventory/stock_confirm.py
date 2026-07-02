from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import InventoryBatchItem, InventoryItem, StockLedger


SHIRT_SIZE_ORDER = {
    "XS": 1,
    "S": 2,
    "M": 3,
    "L": 4,
    "XL": 5,
    "XXL": 6,
    "XXXL": 7,
}


def _size_sort_value(size_name, fallback=9999):
    normalized = (size_name or "").strip().upper()
    return SHIRT_SIZE_ORDER.get(normalized, 100 + int(fallback or 9999))


def _variant_key(item_id, color_id=None, size_id=None, is_material=False):
    if is_material:
        return f"M:{item_id}"
    return f"S:{item_id}:{color_id or 0}:{size_id or 0}"


def _product_group_key(item_id, color_id=None, is_material=False):
    """One checkbox key for one displayed product card."""
    if is_material:
        return f"P:M:{item_id}"
    return f"P:S:{item_id}:{color_id or 0}"


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
                "field_key": key.replace(":", "_"),
                "group_key": _product_group_key(
                    row.item_id,
                    row.color_id,
                    is_material=is_material,
                ),
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
                "group_key": first["group_key"],
                "group_field_key": first["group_key"].replace(":", "_"),
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


def _decimal_text(value):
    value = Decimal(value or 0)
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def _variant_label(variant):
    parts = [variant["item_name"]]
    if not variant["is_material"]:
        parts.extend([variant["color_name"], variant["size_name"]])
    return " / ".join(parts)


def _posted_variant_qty(request, variant):
    raw_qty = (request.POST.get(f'real_qty_{variant["field_key"]}') or "").strip()
    try:
        return Decimal(raw_qty)
    except (InvalidOperation, TypeError):
        raise ValueError(f'Enter a valid real quantity for {_variant_label(variant)}.')


def _posted_variant_note(request, variant):
    return (request.POST.get(f'note_{variant["field_key"]}') or "").strip()


def _store_confirmation_report(request, results):
    wrong_rows = [row for row in results if row["before"] != row["after"]]
    request.session["stock_confirm_last_report"] = {
        "total": len(results),
        "correct": len(results) - len(wrong_rows),
        "wrong": len(wrong_rows),
        "updated": len(wrong_rows),
        "wrong_rows": [
            {
                "label": row["label"],
                "before": _decimal_text(row["before"]),
                "after": _decimal_text(row["after"]),
            }
            for row in wrong_rows[:12]
        ],
        "wrong_more_count": max(len(wrong_rows) - 12, 0),
    }
    request.session.modified = True


@login_required
@permission_required("inventory.add_stockledger", raise_exception=True)
@transaction.atomic
def stock_confirm(request):
    shirt_groups, material_rows, variants = _collect_stock_data()

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        single_correct_key = (request.POST.get("single_correct_key") or "").strip()
        single_update_key = (request.POST.get("single_update_key") or "").strip()

        try:
            results = []

            if single_correct_key or single_update_key:
                key = single_correct_key or single_update_key
                variant = variants.get(key)

                if not variant:
                    messages.error(request, "The selected stock row was not found.")
                    return redirect("stock_confirm")

                real_qty = (
                    variant["current_qty"]
                    if single_correct_key
                    else _posted_variant_qty(request, variant)
                )
                note = _posted_variant_note(request, variant)

                before, after = _confirm_variant(
                    item_id=variant["item_id"],
                    color_id=variant["color_id"],
                    size_id=variant["size_id"],
                    is_material=variant["is_material"],
                    real_qty=real_qty,
                    user=request.user,
                    note=note,
                )
                results.append(
                    {
                        "label": _variant_label(variant),
                        "before": before,
                        "after": after,
                    }
                )

            elif action in {"confirm_selected", "confirm_all"}:
                if action == "confirm_all":
                    selected_keys = list(variants.keys())
                else:
                    selected_keys = []
                    seen = set()
                    for key in request.POST.getlist("selected_keys"):
                        key = key.strip()
                        if key and key not in seen:
                            selected_keys.append(key)
                            seen.add(key)

                if not selected_keys:
                    messages.error(request, "Tick at least one size or material first.")
                    return redirect("stock_confirm")

                selected_variants = []
                for key in selected_keys:
                    variant = variants.get(key)
                    if variant:
                        selected_variants.append(variant)

                if not selected_variants:
                    messages.error(request, "The ticked stock rows were not found.")
                    return redirect("stock_confirm")

                # Validate all selected values first. This prevents a partial
                # update when several sizes are submitted together and one
                # entered quantity is invalid.
                prepared_updates = []

                for variant in selected_variants:
                    prepared_updates.append(
                        {
                            "variant": variant,
                            "real_qty": _posted_variant_qty(request, variant),
                            "note": _posted_variant_note(request, variant),
                        }
                    )

                for prepared in prepared_updates:
                    variant = prepared["variant"]

                    before, after = _confirm_variant(
                        item_id=variant["item_id"],
                        color_id=variant["color_id"],
                        size_id=variant["size_id"],
                        is_material=variant["is_material"],
                        real_qty=prepared["real_qty"],
                        user=request.user,
                        note=prepared["note"],
                    )
                    results.append(
                        {
                            "label": _variant_label(variant),
                            "before": before,
                            "after": after,
                        }
                    )

            else:
                messages.error(request, "Invalid stock confirmation action.")
                return redirect("stock_confirm")

            _store_confirmation_report(request, results)
            wrong_count = sum(1 for row in results if row["before"] != row["after"])
            correct_count = len(results) - wrong_count

            messages.success(
                request,
                (
                    f"{len(results)} item(s) checked: "
                    f"{correct_count} correct and {wrong_count} wrong."
                ),
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
            "product_count": len(shirt_groups) + len(material_rows),
            "shirt_count": sum(len(group["rows"]) for group in shirt_groups),
            "material_count": len(material_rows),
            "last_report": request.session.pop("stock_confirm_last_report", None),
        },
    )


def _report_variant_identity(log):
    item = log.batch_item.item
    is_material = item.item_type != InventoryItem.TYPE_SHIRT
    return _variant_key(
        item.id,
        log.batch_item.color_id,
        log.batch_item.size_id,
        is_material=is_material,
    )


def _report_row(log, recheck_count=1):
    batch_item = log.batch_item
    item = batch_item.item
    is_material = item.item_type != InventoryItem.TYPE_SHIRT
    before = Decimal(log.qty_before or 0)
    after = Decimal(log.qty_after or 0)
    difference = after - before

    return {
        "created_at": log.created_at,
        "item_name": item.name,
        "item_code": item.code,
        "type_label": item.get_item_type_display(),
        "color_name": "-" if is_material else (batch_item.color.name if batch_item.color else "-"),
        "size_name": (
            item.get_unit_display()
            if is_material
            else (batch_item.size.name if batch_item.size else "-")
        ),
        "before": before,
        "after": after,
        "before_text": _decimal_text(before),
        "after_text": _decimal_text(after),
        "difference": difference,
        "difference_text": _decimal_text(difference),
        "is_wrong": difference != 0,
        "checked_by": log.created_by,
        "note": log.correct_remark or log.remark or "",
        "recheck_count": recheck_count,
    }


@login_required
@permission_required("inventory.view_stockledger", raise_exception=True)
def stock_summary_report(request):
    today = timezone.localdate()
    default_from = today.replace(day=1)

    date_from = parse_date((request.GET.get("date_from") or "").strip()) or default_from
    date_to = parse_date((request.GET.get("date_to") or "").strip()) or today

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    keyword = (request.GET.get("q") or "").strip()
    result_filter = (request.GET.get("result") or "ALL").strip().upper()
    if result_filter not in {"ALL", "CORRECT", "WRONG"}:
        result_filter = "ALL"

    logs = (
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
            created_at__date__gte=date_from,
            created_at__date__lte=date_to,
        )
        .order_by("created_at", "id")
    )

    latest_by_day_variant = {}
    check_counts = defaultdict(int)

    for log in logs:
        local_created = timezone.localtime(log.created_at) if timezone.is_aware(log.created_at) else log.created_at
        day = local_created.date()
        identity = _report_variant_identity(log)
        key = (day, identity)
        check_counts[key] += 1
        latest_by_day_variant[key] = log

    grouped_days = defaultdict(list)

    for (day, identity), log in latest_by_day_variant.items():
        row = _report_row(log, recheck_count=check_counts[(day, identity)])

        if keyword:
            staff_name = ""
            if row["checked_by"]:
                staff_name = (
                    f"{row['checked_by'].username} "
                    f"{row['checked_by'].first_name} "
                    f"{row['checked_by'].last_name}"
                )
            searchable = " ".join(
                [
                    row["item_name"],
                    row["item_code"],
                    row["type_label"],
                    row["color_name"],
                    row["size_name"],
                    staff_name,
                    row["note"],
                ]
            ).lower()
            if keyword.lower() not in searchable:
                continue

        if result_filter == "CORRECT" and row["is_wrong"]:
            continue
        if result_filter == "WRONG" and not row["is_wrong"]:
            continue

        grouped_days[day].append(row)

    daily_reports = []
    total_checked = 0
    total_correct = 0
    total_wrong = 0
    total_added = Decimal("0")
    total_removed = Decimal("0")

    for day in sorted(grouped_days.keys(), reverse=True):
        rows = grouped_days[day]
        rows.sort(
            key=lambda row: (
                0 if row["is_wrong"] else 1,
                row["item_code"],
                row["item_name"],
                row["color_name"],
                row["size_name"],
            )
        )

        correct = sum(1 for row in rows if not row["is_wrong"])
        wrong = len(rows) - correct
        added = sum((row["difference"] for row in rows if row["difference"] > 0), Decimal("0"))
        removed = sum((abs(row["difference"]) for row in rows if row["difference"] < 0), Decimal("0"))
        rechecked = sum(1 for row in rows if row["recheck_count"] > 1)

        daily_reports.append(
            {
                "date": day,
                "rows": rows,
                "total": len(rows),
                "correct": correct,
                "wrong": wrong,
                "added_qty_text": _decimal_text(added),
                "removed_qty_text": _decimal_text(removed),
                "rechecked": rechecked,
            }
        )

        total_checked += len(rows)
        total_correct += correct
        total_wrong += wrong
        total_added += added
        total_removed += removed

    return render(
        request,
        "inventory/stock_summary_report.html",
        {
            "daily_reports": daily_reports,
            "day_count": len(daily_reports),
            "total_checked": total_checked,
            "total_correct": total_correct,
            "total_wrong": total_wrong,
            "total_added_text": _decimal_text(total_added),
            "total_removed_text": _decimal_text(total_removed),
            "keyword": keyword,
            "date_from": date_from,
            "date_to": date_to,
            "result_filter": result_filter,
        },
    )


def _history_day_bounds(report_date):
    """
    Return the start of the selected local date and the start of the next date.
    This keeps records grouped according to the project's active timezone.
    """
    start = datetime.combine(report_date, time.min)
    end = start + timedelta(days=1)

    if timezone.is_aware(timezone.now()):
        current_tz = timezone.get_current_timezone()
        start = timezone.make_aware(start, current_tz)
        end = timezone.make_aware(end, current_tz)

    return start, end



def _history_variant_identity(log):
    """
    One stock type means:
    - Cloth: item + color + size
    - Material: item
    """
    item = log.batch_item.item

    if item.item_type != InventoryItem.TYPE_SHIRT:
        return f"M:{item.id}"

    return (
        f"S:{item.id}:"
        f"{log.batch_item.color_id or 0}:"
        f"{log.batch_item.size_id or 0}"
    )


def _history_row_from_log(log):
    before = Decimal(log.qty_before or 0)
    after = Decimal(log.qty_after or 0)
    difference = after - before

    item = log.batch_item.item
    is_material = item.item_type != InventoryItem.TYPE_SHIRT

    local_created_at = (
        timezone.localtime(log.created_at)
        if timezone.is_aware(log.created_at)
        else log.created_at
    )

    return {
        "created_at": local_created_at,
        "type_label": item.get_item_type_display(),
        "is_material": is_material,
        "item_name": item.name,
        "item_code": item.code,
        "color_name": (
            "-"
            if is_material
            else (
                log.batch_item.color.name
                if log.batch_item.color
                else "-"
            )
        ),
        "size_name": (
            item.get_unit_display()
            if is_material
            else (
                log.batch_item.size.name
                if log.batch_item.size
                else "-"
            )
        ),
        "before": before,
        "after": after,
        "before_text": _decimal_text(before),
        "after_text": _decimal_text(after),
        "difference": difference,
        "difference_text": _decimal_text(difference),
        "is_wrong_count": difference != 0,
        "note": log.correct_remark or log.remark or "",
        "created_by": log.created_by,
    }


@login_required
@permission_required("inventory.view_stockledger", raise_exception=True)
def stock_history(request):
    """
    First page: one row per stock-count date.

    Counts are stock TYPES, not cloth quantity:
    - Cloth type = item + color + size
    - Material type = item

    If the same type is checked more than once on the same date,
    only the latest check is used in that date's totals.
    """
    today = timezone.localdate()
    default_from = today.replace(day=1)

    date_from = parse_date((request.GET.get("date_from") or "").strip()) or default_from
    date_to = parse_date((request.GET.get("date_to") or "").strip()) or today

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    start_at, _ = _history_day_bounds(date_from)
    _, end_at = _history_day_bounds(date_to)

    logs = (
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
            created_at__gte=start_at,
            created_at__lt=end_at,
        )
        .order_by("-created_at", "-id")
    )

    # Because the query is newest first, setdefault keeps the latest
    # check for each type on each date.
    latest_by_day_type = {}

    for log in logs:
        row = _history_row_from_log(log)
        report_date = row["created_at"].date()
        identity = _history_variant_identity(log)
        latest_by_day_type.setdefault((report_date, identity), row)

    grouped_days = defaultdict(list)

    for (report_date, _identity), row in latest_by_day_type.items():
        grouped_days[report_date].append(row)

    daily_reports = []
    range_total = 0
    range_cloth = 0
    range_material = 0
    range_correct = 0
    range_wrong = 0

    for report_date in sorted(grouped_days.keys(), reverse=True):
        rows = grouped_days[report_date]

        rows.sort(
            key=lambda row: (
                row["item_code"],
                row["item_name"],
                row["color_name"],
                row["size_name"],
            )
        )

        cloth_count = sum(1 for row in rows if not row["is_material"])
        material_count = len(rows) - cloth_count
        correct = sum(1 for row in rows if not row["is_wrong_count"])
        wrong = len(rows) - correct

        daily_reports.append(
            {
                "date": report_date,
                "total": len(rows),
                "cloth_count": cloth_count,
                "material_count": material_count,
                "correct": correct,
                "wrong": wrong,
            }
        )

        range_total += len(rows)
        range_cloth += cloth_count
        range_material += material_count
        range_correct += correct
        range_wrong += wrong

    return render(
        request,
        "inventory/stock_history.html",
        {
            "daily_reports": daily_reports,
            "day_count": len(daily_reports),
            "date_from": date_from,
            "date_to": date_to,
            "range_total": range_total,
            "range_cloth": range_cloth,
            "range_material": range_material,
            "range_correct": range_correct,
            "range_wrong": range_wrong,
        },
    )


@login_required
@permission_required("inventory.view_stockledger", raise_exception=True)
def stock_history_detail(request, year, month, day):
    """
    Detail page for one date.

    One displayed row represents one stock type. If the same type was
    checked repeatedly on the selected date, its latest check is shown.
    """
    try:
        report_date = datetime(year, month, day).date()
    except ValueError as exc:
        raise Http404("Invalid stock history date.") from exc

    start_at, end_at = _history_day_bounds(report_date)

    logs = (
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
            created_at__gte=start_at,
            created_at__lt=end_at,
        )
        .order_by("-created_at", "-id")
    )

    latest_by_type = {}

    for log in logs:
        identity = _history_variant_identity(log)
        latest_by_type.setdefault(identity, _history_row_from_log(log))

    all_rows = list(latest_by_type.values())
    all_rows.sort(key=lambda row: row["created_at"], reverse=True)

    total_count = len(all_rows)
    cloth_count = sum(1 for row in all_rows if not row["is_material"])
    material_count = total_count - cloth_count
    correct_count = sum(1 for row in all_rows if not row["is_wrong_count"])
    wrong_count = total_count - correct_count

    total_added = sum(
        (row["difference"] for row in all_rows if row["difference"] > 0),
        Decimal("0"),
    )
    total_removed = sum(
        (abs(row["difference"]) for row in all_rows if row["difference"] < 0),
        Decimal("0"),
    )

    keyword = (request.GET.get("q") or "").strip()
    result_filter = (request.GET.get("result") or "ALL").strip().upper()

    if result_filter not in {"ALL", "CORRECT", "WRONG"}:
        result_filter = "ALL"

    visible_rows = []

    for row in all_rows:
        if result_filter == "CORRECT" and row["is_wrong_count"]:
            continue

        if result_filter == "WRONG" and not row["is_wrong_count"]:
            continue

        if keyword:
            staff_name = ""

            if row["created_by"]:
                staff_name = " ".join(
                    [
                        row["created_by"].username or "",
                        row["created_by"].first_name or "",
                        row["created_by"].last_name or "",
                    ]
                )

            searchable = " ".join(
                [
                    row["item_name"],
                    row["item_code"],
                    row["type_label"],
                    row["color_name"],
                    row["size_name"],
                    staff_name,
                    row["note"],
                ]
            ).lower()

            if keyword.lower() not in searchable:
                continue

        visible_rows.append(row)

    return render(
        request,
        "inventory/stock_history_detail.html",
        {
            "report_date": report_date,
            "rows": visible_rows,
            "visible_count": len(visible_rows),
            "keyword": keyword,
            "result_filter": result_filter,
            "total_count": total_count,
            "cloth_count": cloth_count,
            "material_count": material_count,
            "correct_count": correct_count,
            "wrong_count": wrong_count,
            "total_added_text": _decimal_text(total_added),
            "total_removed_text": _decimal_text(total_removed),
        },
    )
