from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from inventory.models import InventoryBatch, InventoryBatchItem, InventoryItem
from .models import StockConsumption


def _get_fifo_rows(item, color=None, size=None):
    rows = InventoryBatchItem.objects.select_for_update().filter(
        item=item,
        is_active=True,
        qty_remaining__gt=0,
        batch__is_deleted=False,
    )

    if color:
        rows = rows.filter(color=color)

    if size:
        rows = rows.filter(size=size)

    return rows.order_by("batch__received_date", "id")


def _variant_text(item, color=None, size=None):
    text = str(item)

    if color:
        text += f" / {color.name}"

    if size:
        text += f" / {size.name}"

    return text


def _available_fifo_qty(item, color=None, size=None):
    total = Decimal("0")

    for row in _get_fifo_rows(item, color=color, size=size):
        total += Decimal(row.qty_remaining or 0)

    return total


def _retail_color_size_for_line(order, line):
    if order.service_type != order.SERVICE_RETAIL:
        return line.color, line.size

    # Retail material → no color/size
    if line.material_item:
        return None, None

    return line.color, line.size


def get_order_shortages(order):
    shortages = []

    # Film Only and Print & Heat Press do not deduct stock
    if order.service_type in [order.SERVICE_FILM_ONLY, order.SERVICE_PRINT_HEATPRESS]:
        return shortages

    lines = order.items.select_related(
        "shirt_item",
        "material_item",
        "film_item",
        "color",
        "size",
    )

    for line in lines:
        needed = Decimal(line.quantity or 0)

        if needed <= 0:
            continue

        # Retail material stock
        if order.service_type == order.SERVICE_RETAIL and line.material_item:
            stock_item = line.material_item
            color = None
            size = None

        # Full order / Retail shirt stock
        elif line.shirt_item:
            stock_item = line.shirt_item
            color, size = _retail_color_size_for_line(order, line)

        else:
            continue

        available = _available_fifo_qty(
            stock_item,
            color=color,
            size=size,
        )

        shortage = needed - available

        if shortage > 0:
            shortages.append(
                {
                    "type": "stock",
                    "label": _variant_text(stock_item, color, size),
                    "needed": needed,
                    "available": available,
                    "shortage": shortage,
                }
            )

    return shortages

def build_shortage_message(shortages):
    if not shortages:
        return ""

    lines = ["Stock is not enough for:"]

    for s in shortages:
        lines.append(
            f"- {s['label']} : need {s['needed']}, available {s['available']}, short {s['shortage']}"
        )

    return "\n".join(lines)


def _get_or_create_shortage_batch():
    batch, _ = InventoryBatch.objects.get_or_create(
        batch_no="AUTO-SHORTAGE",
        defaults={
            "supplier": "Auto Shortage",
            "received_date": timezone.localdate(),
            "status": InventoryBatch.STATUS_FINAL,
            "note": "Auto-created for negative stock when order is created without enough stock.",
        },
    )
    return batch


def _get_or_create_negative_row(item, color=None, size=None):
    batch = _get_or_create_shortage_batch()

    row = (
        InventoryBatchItem.objects.select_for_update()
        .filter(
            batch=batch,
            item=item,
            color=color,
            size=size,
            is_active=True,
        )
        .first()
    )

    if row:
        return row

    return InventoryBatchItem.objects.create(
        batch=batch,
        item=item,
        color=color,
        size=size,
        qty_received=Decimal("0"),
        qty_remaining=Decimal("0"),
        base_unit_cost=Decimal("0"),
        final_unit_cost=Decimal("0"),
        is_active=True,
    )


def _consume_fifo(item, qty_needed, order, order_item, color=None, size=None, allow_shortage=False):
    qty_needed = Decimal(qty_needed or 0)

    if qty_needed <= 0:
        return Decimal("0")

    rows = _get_fifo_rows(item, color=color, size=size)
    remaining = qty_needed

    for row in rows:
        if remaining <= 0:
            break

        available = Decimal(row.qty_remaining or 0)
        take_qty = min(available, remaining)

        if take_qty > 0:
            row.qty_remaining = available - take_qty
            row.save(update_fields=["qty_remaining"])

            StockConsumption.objects.create(
                order=order,
                order_item=order_item,
                batch_item=row,
                consumed_qty=take_qty,
                unit_cost=row.final_unit_cost or row.base_unit_cost or 0,
            )

            remaining -= take_qty

    if remaining > 0:
        if not allow_shortage:
            raise ValidationError(f"Not enough stock for {_variant_text(item, color, size)}.")

        negative_row = _get_or_create_negative_row(item, color=color, size=size)

        before_qty = Decimal(negative_row.qty_remaining or 0)
        negative_row.qty_remaining = before_qty - remaining
        negative_row.save(update_fields=["qty_remaining"])

        StockConsumption.objects.create(
            order=order,
            order_item=order_item,
            batch_item=negative_row,
            consumed_qty=remaining,
            unit_cost=negative_row.final_unit_cost or negative_row.base_unit_cost or 0,
        )

        remaining = Decimal("0")

    return remaining


@transaction.atomic
def deduct_stock_for_order(order, allow_shortage=False):
    if order.stock_deducted:
        return []

    # Film Only and Print & Heat Press do not deduct stock
    if order.service_type in [order.SERVICE_FILM_ONLY, order.SERVICE_PRINT_HEATPRESS]:
        return []

    shortages = get_order_shortages(order)

    if shortages and not allow_shortage:
        raise ValidationError(build_shortage_message(shortages))

    unresolved = []

    lines = order.items.select_related(
        "shirt_item",
        "material_item",
        "film_item",
        "color",
        "size",
    )

    for line in lines:
        qty_needed = Decimal(line.quantity or 0)

        if qty_needed <= 0:
            continue

        if order.service_type == order.SERVICE_RETAIL and line.material_item:
            stock_item = line.material_item
            color = None
            size = None
        elif line.shirt_item:
            stock_item = line.shirt_item
            color, size = _retail_color_size_for_line(order, line)
        else:
            continue

        remaining = _consume_fifo(
            stock_item,
            qty_needed,
            order,
            line,
            color=color,
            size=size,
            allow_shortage=allow_shortage,
        )

        if remaining > 0:
            unresolved.append({
                "type": "stock",
                "label": _variant_text(stock_item, color, size),
                "shortage": remaining,
            })

    order.stock_deducted = True
    order.save(update_fields=["stock_deducted"])

    return unresolved

@transaction.atomic
def restore_stock_for_order(order):
    consumptions = order.stock_consumptions.select_related("batch_item").order_by("-id")

    for consume in consumptions:
        row = consume.batch_item
        row.qty_remaining = Decimal(row.qty_remaining or 0) + Decimal(consume.consumed_qty or 0)
        row.save(update_fields=["qty_remaining"])

    consumptions.delete()

    order.stock_deducted = False
    order.save(update_fields=["stock_deducted"])