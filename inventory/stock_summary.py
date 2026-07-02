from collections import defaultdict
from decimal import Decimal

from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import InventoryItem, StockLedger


def _decimal_text(value):
    value = Decimal(value or 0)
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def _variant_identity(log):
    item = log.batch_item.item
    is_material = item.item_type != InventoryItem.TYPE_SHIRT

    if is_material:
        return f"M:{item.id}"

    return (
        f"S:{item.id}:"
        f"{log.batch_item.color_id or 0}:"
        f"{log.batch_item.size_id or 0}"
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
        "color_name": (
            "-"
            if is_material
            else (batch_item.color.name if batch_item.color else "-")
        ),
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
        local_created = (
            timezone.localtime(log.created_at)
            if timezone.is_aware(log.created_at)
            else log.created_at
        )
        day = local_created.date()
        identity = _variant_identity(log)
        key = (day, identity)

        check_counts[key] += 1
        latest_by_day_variant[key] = log

    grouped_days = defaultdict(list)

    for (day, identity), log in latest_by_day_variant.items():
        row = _report_row(
            log,
            recheck_count=check_counts[(day, identity)],
        )

        if keyword:
            staff_name = ""

            if row["checked_by"]:
                staff_name = " ".join(
                    [
                        row["checked_by"].username or "",
                        row["checked_by"].first_name or "",
                        row["checked_by"].last_name or "",
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

        added = sum(
            (row["difference"] for row in rows if row["difference"] > 0),
            Decimal("0"),
        )
        removed = sum(
            (abs(row["difference"]) for row in rows if row["difference"] < 0),
            Decimal("0"),
        )
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
