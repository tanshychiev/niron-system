from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import StockLedger


def _safe_user(user):
    if user and getattr(user, "is_authenticated", False):
        return user
    return None


def log_stock_movement(
    *,
    batch_item,
    movement_type,
    qty_before,
    qty_after,
    user=None,
    reference_no="",
    order=None,
    batch=None,
    source_type="",
    source_id=None,
    remark="",
    is_correct_checkpoint=False,
    correct_remark="",
):
    """
    Main stock ledger logger.

    It records:
    - before qty
    - in qty
    - out qty
    - after qty
    - invoice/order/batch/reference
    - who changed it
    - remark
    - correct checkpoint
    """
    qty_before = Decimal(qty_before or 0)
    qty_after = Decimal(qty_after or 0)

    diff = qty_after - qty_before

    qty_in = diff if diff > 0 else Decimal("0")
    qty_out = abs(diff) if diff < 0 else Decimal("0")

    if order:
        if not reference_no:
            reference_no = order.order_no or f"ORDER-{order.id}"
        if not source_type:
            source_type = StockLedger.SOURCE_ORDER
        if source_id is None:
            source_id = order.id

    if batch:
        if not reference_no:
            reference_no = batch.batch_no or f"BATCH-{batch.id}"
        if not source_type:
            source_type = StockLedger.SOURCE_STOCK_IN
        if source_id is None:
            source_id = batch.id

    if not source_type:
        if is_correct_checkpoint:
            source_type = StockLedger.SOURCE_CORRECT
        else:
            source_type = StockLedger.SOURCE_OTHER

    return StockLedger.objects.create(
        batch_item=batch_item,
        movement_type=movement_type,
        qty_before=qty_before,
        qty_in=qty_in,
        qty_out=qty_out,
        qty_after=qty_after,
        reference_no=reference_no or "",
        source_type=source_type or "",
        source_id=source_id,
        order_id=order.id if order else None,
        order_no=order.order_no if order else "",
        batch_no=batch.batch_no if batch else getattr(batch_item.batch, "batch_no", ""),
        remark=remark or "",
        is_correct_checkpoint=is_correct_checkpoint,
        correct_remark=correct_remark or "",
        created_by=_safe_user(user),
        created_at=timezone.now(),
    )


@transaction.atomic
def correct_stock_count(
    *,
    batch_item,
    correct_qty,
    user=None,
    remark="Correct stock count",
):
    """
    Use this after real physical stock count is confirmed.

    This is the checkpoint.
    Next time stock is wrong, check only from this correct date forward.
    """
    correct_qty = Decimal(correct_qty or 0)

    qty_before = Decimal(batch_item.qty_remaining or 0)

    batch_item.qty_remaining = correct_qty
    batch_item.save(update_fields=["qty_remaining"])

    log_stock_movement(
        batch_item=batch_item,
        movement_type=StockLedger.TYPE_CORRECT,
        qty_before=qty_before,
        qty_after=correct_qty,
        user=user,
        reference_no=f"CORRECT-{batch_item.id}",
        source_type=StockLedger.SOURCE_CORRECT,
        source_id=batch_item.id,
        remark=remark,
        is_correct_checkpoint=True,
        correct_remark=remark,
    )

    return batch_item


def log_order_out(
    *,
    batch_item,
    qty_before,
    qty_after,
    order,
    user=None,
    remark="",
):
    """
    When invoice/order deducts stock.
    Example: NR-2605-001 OUT 20 pcs.
    """
    return log_stock_movement(
        batch_item=batch_item,
        movement_type=StockLedger.TYPE_ORDER_OUT,
        qty_before=qty_before,
        qty_after=qty_after,
        user=user or getattr(order, "created_by", None),
        reference_no=order.order_no,
        order=order,
        source_type=StockLedger.SOURCE_ORDER,
        source_id=order.id,
        remark=remark or f"Order / invoice out: {order.order_no}",
    )


def log_order_restore(
    *,
    batch_item,
    qty_before,
    qty_after,
    order,
    user=None,
    remark="",
):
    """
    When order cancel/edit restores stock back.
    Example: NR-2605-001 RESTORE 20 pcs.
    """
    return log_stock_movement(
        batch_item=batch_item,
        movement_type=StockLedger.TYPE_ORDER_RESTORE,
        qty_before=qty_before,
        qty_after=qty_after,
        user=user or getattr(order, "created_by", None),
        reference_no=order.order_no,
        order=order,
        source_type=StockLedger.SOURCE_ORDER,
        source_id=order.id,
        remark=remark or f"Order / invoice restore: {order.order_no}",
    )


def log_stock_in(
    *,
    batch_item,
    qty_before,
    qty_after,
    batch=None,
    user=None,
    remark="",
):
    """
    When stock in adds stock.
    Example: STK-20260515 IN 100 pcs.
    """
    batch = batch or batch_item.batch

    return log_stock_movement(
        batch_item=batch_item,
        movement_type=StockLedger.TYPE_STOCK_IN,
        qty_before=qty_before,
        qty_after=qty_after,
        user=user or getattr(batch, "created_by", None),
        reference_no=batch.batch_no,
        batch=batch,
        source_type=StockLedger.SOURCE_STOCK_IN,
        source_id=batch.id,
        remark=remark or f"Stock in: {batch.batch_no}",
    )


def log_adjustment(
    *,
    batch_item,
    qty_before,
    qty_after,
    adjustment=None,
    user=None,
    remark="",
):
    """
    When manual adjustment changes stock.
    Auto detects IN or OUT.
    """
    qty_before = Decimal(qty_before or 0)
    qty_after = Decimal(qty_after or 0)

    movement_type = StockLedger.TYPE_ADJUST_IN
    if qty_after < qty_before:
        movement_type = StockLedger.TYPE_ADJUST_OUT

    reference_no = ""
    source_id = None

    if adjustment:
        reference_no = f"ADJ-{adjustment.id}"
        source_id = adjustment.id

    return log_stock_movement(
        batch_item=batch_item,
        movement_type=movement_type,
        qty_before=qty_before,
        qty_after=qty_after,
        user=user or getattr(adjustment, "created_by", None),
        reference_no=reference_no,
        source_type=StockLedger.SOURCE_ADJUSTMENT,
        source_id=source_id,
        remark=remark or getattr(adjustment, "reason", "") or "Stock adjustment",
    )


def log_batch_edit(
    *,
    batch_item,
    qty_before,
    qty_after,
    batch=None,
    user=None,
    remark="",
):
    """
    When stock batch row is edited and qty_remaining changes.
    """
    batch = batch or batch_item.batch

    return log_stock_movement(
        batch_item=batch_item,
        movement_type=StockLedger.TYPE_BATCH_EDIT,
        qty_before=qty_before,
        qty_after=qty_after,
        user=user or getattr(batch, "updated_by", None),
        reference_no=batch.batch_no,
        batch=batch,
        source_type=StockLedger.SOURCE_BATCH,
        source_id=batch.id,
        remark=remark or f"Batch edited: {batch.batch_no}",
    )


def log_batch_delete(
    *,
    batch_item,
    qty_before,
    qty_after,
    batch=None,
    user=None,
    remark="",
):
    """
    When batch is deleted/hidden and stock should be traceable.
    """
    batch = batch or batch_item.batch

    return log_stock_movement(
        batch_item=batch_item,
        movement_type=StockLedger.TYPE_BATCH_DELETE,
        qty_before=qty_before,
        qty_after=qty_after,
        user=user or getattr(batch, "deleted_by", None),
        reference_no=batch.batch_no,
        batch=batch,
        source_type=StockLedger.SOURCE_BATCH,
        source_id=batch.id,
        remark=remark or f"Batch deleted: {batch.batch_no}",
    )