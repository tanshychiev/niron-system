from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from inventory.models import InventoryBatch, InventoryBatchItem, StockLedger
from .models import StockConsumption


def _dec(value):
    return Decimal(str(value or "0"))


def _get_fifo_rows(item, color=None, size=None):
    rows = InventoryBatchItem.objects.select_for_update().filter(
        item=item,
        is_active=True,
        qty_remaining__gt=0,
        batch__is_deleted=False,
    )

    if color:
        rows = rows.filter(color=color)
    else:
        rows = rows.filter(color__isnull=True)

    if size:
        rows = rows.filter(size=size)
    else:
        rows = rows.filter(size__isnull=True)

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
        total += _dec(row.qty_remaining)

    return total


def _retail_color_size_for_line(order, line):
    if order.service_type != order.SERVICE_RETAIL:
        return line.color, line.size

    # Retail material has no color / size.
    if line.material_item:
        return None, None

    return line.color, line.size


def _ledger_user(order, user=None):
    if user and getattr(user, "is_authenticated", False):
        return user

    if getattr(order, "created_by", None):
        return order.created_by

    return None


def _log_order_out(order, order_item, batch_item, qty, qty_before, qty_after, user=None):
    StockLedger.objects.create(
        batch_item=batch_item,
        movement_type=StockLedger.TYPE_ORDER_OUT,
        qty_before=qty_before,
        qty_in=Decimal("0"),
        qty_out=qty,
        qty_after=qty_after,
        reference_no=order.order_no or "",
        source_type=StockLedger.SOURCE_ORDER,
        source_id=order.id,
        order_id=order.id,
        order_no=order.order_no or "",
        batch_no=batch_item.batch.batch_no if batch_item.batch else "",
        remark=f"Order stock out: {order.order_no}",
        created_by=_ledger_user(order, user),
    )


def _log_order_restore(order, consume, qty, qty_before, qty_after, user=None):
    batch_item = consume.batch_item

    StockLedger.objects.create(
        batch_item=batch_item,
        movement_type=StockLedger.TYPE_ORDER_RESTORE,
        qty_before=qty_before,
        qty_in=qty,
        qty_out=Decimal("0"),
        qty_after=qty_after,
        reference_no=order.order_no or "",
        source_type=StockLedger.SOURCE_ORDER,
        source_id=order.id,
        order_id=order.id,
        order_no=order.order_no or "",
        batch_no=batch_item.batch.batch_no if batch_item.batch else "",
        remark=f"Order stock restored: {order.order_no}",
        created_by=_ledger_user(order, user),
    )


def get_order_shortages(order):
    shortages = []

    # Film Only and Print & Heat Press do not deduct stock.
    if order.service_type in [
        order.SERVICE_FILM_ONLY,
        order.SERVICE_PRINT_HEATPRESS,
    ]:
        return shortages

    lines = order.items.select_related(
        "shirt_item",
        "material_item",
        "film_item",
        "color",
        "size",
    )

    for line in lines:
        needed = _dec(line.quantity)

        if needed <= 0:
            continue

        # Retail material stock.
        if order.service_type == order.SERVICE_RETAIL and line.material_item:
            stock_item = line.material_item
            color = None
            size = None

        # Full order / Retail shirt stock.
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
            f"- {s['label']} : need {s['needed']}, "
            f"available {s['available']}, short {s['shortage']}"
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


def _consume_fifo(
    item,
    qty_needed,
    order,
    order_item,
    color=None,
    size=None,
    allow_shortage=False,
    user=None,
):
    qty_needed = _dec(qty_needed)

    if qty_needed <= 0:
        return Decimal("0")

    rows = _get_fifo_rows(item, color=color, size=size)
    remaining = qty_needed

    for row in rows:
        if remaining <= 0:
            break

        available = _dec(row.qty_remaining)
        take_qty = min(available, remaining)

        if take_qty <= 0:
            continue

        before_qty = available
        after_qty = available - take_qty

        row.qty_remaining = after_qty
        row.save(update_fields=["qty_remaining"])

        StockConsumption.objects.create(
            order=order,
            order_item=order_item,
            batch_item=row,
            consumed_qty=take_qty,
            unit_cost=row.final_unit_cost or row.base_unit_cost or Decimal("0"),
        )

        _log_order_out(
            order=order,
            order_item=order_item,
            batch_item=row,
            qty=take_qty,
            qty_before=before_qty,
            qty_after=after_qty,
            user=user,
        )

        remaining -= take_qty

    if remaining > 0:
        if not allow_shortage:
            raise ValidationError(
                f"Not enough stock for {_variant_text(item, color, size)}."
            )

        negative_row = _get_or_create_negative_row(
            item,
            color=color,
            size=size,
        )

        before_qty = _dec(negative_row.qty_remaining)
        after_qty = before_qty - remaining

        negative_row.qty_remaining = after_qty
        negative_row.save(update_fields=["qty_remaining"])

        StockConsumption.objects.create(
            order=order,
            order_item=order_item,
            batch_item=negative_row,
            consumed_qty=remaining,
            unit_cost=negative_row.final_unit_cost or negative_row.base_unit_cost or Decimal("0"),
        )

        _log_order_out(
            order=order,
            order_item=order_item,
            batch_item=negative_row,
            qty=remaining,
            qty_before=before_qty,
            qty_after=after_qty,
            user=user,
        )

        remaining = Decimal("0")

    return remaining


@transaction.atomic
def deduct_stock_for_order(order, allow_shortage=False, user=None):
    if order.stock_deducted:
        return []

    # Film Only and Print & Heat Press do not deduct stock.
    if order.service_type in [
        order.SERVICE_FILM_ONLY,
        order.SERVICE_PRINT_HEATPRESS,
    ]:
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
        qty_needed = _dec(line.quantity)

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
            user=user,
        )

        if remaining > 0:
            unresolved.append(
                {
                    "type": "stock",
                    "label": _variant_text(stock_item, color, size),
                    "shortage": remaining,
                }
            )

    order.stock_deducted = True
    order.save(update_fields=["stock_deducted"])

    return unresolved


@transaction.atomic
def restore_stock_for_order(order, user=None):
    if not order.stock_deducted:
        return

    consumptions = order.stock_consumptions.select_related(
        "batch_item",
        "batch_item__batch",
        "batch_item__item",
        "batch_item__color",
        "batch_item__size",
    ).order_by("-id")

    for consume in consumptions:
        row = consume.batch_item

        before_qty = _dec(row.qty_remaining)
        restore_qty = _dec(consume.consumed_qty)
        after_qty = before_qty + restore_qty

        row.qty_remaining = after_qty
        row.save(update_fields=["qty_remaining"])

        _log_order_restore(
            order=order,
            consume=consume,
            qty=restore_qty,
            qty_before=before_qty,
            qty_after=after_qty,
            user=user,
        )

    consumptions.delete()

    order.stock_deducted = False
    order.save(update_fields=["stock_deducted"])