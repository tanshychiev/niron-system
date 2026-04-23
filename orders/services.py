from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from inventory.models import InventoryBatchItem
from .models import StockConsumption


def _get_fifo_rows(item, color=None, size=None):
    rows = InventoryBatchItem.objects.select_for_update().filter(
        item=item,
        is_active=True,
        qty_remaining__gt=0,
        batch__status="FINAL",
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


def get_order_shortages(order):
    shortages = []

    for line in order.items.select_related("shirt_item", "film_item", "color", "size"):
        if line.shirt_item:
            needed = Decimal(line.quantity or 0)
            available = _available_fifo_qty(
                line.shirt_item,
                color=line.color,
                size=line.size,
            )
            shortage = needed - available
            if shortage > 0:
                shortages.append(
                    {
                        "type": "shirt",
                        "label": _variant_text(line.shirt_item, line.color, line.size),
                        "needed": needed,
                        "available": available,
                        "shortage": shortage,
                    }
                )

        film_qty = Decimal("0")

        if line.manual_film_meter and line.manual_film_meter > 0:
            film_qty += Decimal(line.manual_film_meter)

        if line.film_item and line.film_meter_per_piece and line.quantity:
            film_qty += Decimal(line.film_meter_per_piece) * Decimal(line.quantity)

        if line.film_item and film_qty > 0:
            available = _available_fifo_qty(line.film_item)
            shortage = film_qty - available
            if shortage > 0:
                shortages.append(
                    {
                        "type": "film",
                        "label": str(line.film_item),
                        "needed": film_qty,
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

    if remaining > 0 and not allow_shortage:
        raise ValidationError(f"Not enough stock for {_variant_text(item, color, size)}.")

    return remaining


@transaction.atomic
def deduct_stock_for_order(order, allow_shortage=False):
    if order.stock_deducted:
        return []

    shortages = get_order_shortages(order)

    if shortages and not allow_shortage:
        raise ValidationError(build_shortage_message(shortages))

    unresolved = []

    for line in order.items.select_related("shirt_item", "film_item", "color", "size"):
        if line.shirt_item:
            shirt_remaining = _consume_fifo(
                line.shirt_item,
                line.quantity,
                order,
                line,
                color=line.color,
                size=line.size,
                allow_shortage=allow_shortage,
            )
            if shirt_remaining > 0:
                unresolved.append(
                    {
                        "label": _variant_text(line.shirt_item, line.color, line.size),
                        "shortage": shirt_remaining,
                    }
                )

        film_qty = Decimal("0")

        if line.manual_film_meter and line.manual_film_meter > 0:
            film_qty += Decimal(line.manual_film_meter)

        if line.film_item and line.film_meter_per_piece and line.quantity:
            film_qty += Decimal(line.film_meter_per_piece) * Decimal(line.quantity)

        if line.film_item and film_qty > 0:
            film_remaining = _consume_fifo(
                line.film_item,
                film_qty,
                order,
                line,
                allow_shortage=allow_shortage,
            )
            if film_remaining > 0:
                unresolved.append(
                    {
                        "label": str(line.film_item),
                        "shortage": film_remaining,
                    }
                )

    order.stock_deducted = True
    order.save(update_fields=["stock_deducted"])

    return unresolved