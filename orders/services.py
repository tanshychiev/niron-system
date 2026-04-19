from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from inventory.models import InventoryBatchItem
from .models import StockConsumption


def _consume_fifo(item, qty_needed, order, order_item, color=None, size=None):
    qty_needed = Decimal(qty_needed or 0)
    if qty_needed <= 0:
        return

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

    rows = rows.order_by("batch__received_date", "id")

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
        variant_text = str(item)
        if color:
            variant_text += f" / {color.name}"
        if size:
            variant_text += f" / {size.name}"
        raise ValidationError(f"Not enough stock for {variant_text}.")


@transaction.atomic
def deduct_stock_for_order(order):
    if order.stock_deducted:
        return

    for line in order.items.select_related("shirt_item", "film_item", "color", "size"):
        if line.shirt_item:
            _consume_fifo(
                line.shirt_item,
                line.quantity,
                order,
                line,
                color=line.color,
                size=line.size,
            )

        film_qty = Decimal("0")

        if line.manual_film_meter and line.manual_film_meter > 0:
            film_qty += Decimal(line.manual_film_meter)

        if line.film_item and line.film_meter_per_piece and line.quantity:
            film_qty += Decimal(line.film_meter_per_piece) * Decimal(line.quantity)

        if line.film_item and film_qty > 0:
            _consume_fifo(
                line.film_item,
                film_qty,
                order,
                line,
            )

    order.stock_deducted = True
    order.save(update_fields=["stock_deducted"])