from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from inventory.models import InventoryBatch, InventoryBatchItem
from inventory.stock_ledger import log_stock_in

from .models import (
    CuttingRollUsage,
    FabricRoll,
    ProductionPaymentAllocation,
    ProductionPaymentBatch,
    ProductionPayable,
    ProductionProject,
    SewingJob,
    SewingReturn,
)


ZERO = Decimal("0")


def _fabric_supplier_token(name):
    """Short printable supplier token, e.g. 'Kam' -> 'KAM'."""
    cleaned = "".join(ch for ch in str(name or "").upper() if ch.isalnum())
    return (cleaned[:6] or "SUP")


def _fabric_roll_code(receipt, index, weight):
    """
    Human-friendly printed code:
        01-KAM-230826-24.65

    Roll number is within this fabric/color receipt. If an identical code
    already exists (possible when another color has the same supplier/date/
    roll number/weight), append a small numeric suffix to keep DB uniqueness.
    """
    supplier = _fabric_supplier_token(receipt.supplier)
    date_token = receipt.received_date.strftime("%d%m%y")
    weight_token = f"{Decimal(weight):.2f}"
    base = f"{index:02d}-{supplier}-{date_token}-{weight_token}"
    code = base
    suffix = 2
    while FabricRoll.objects.filter(roll_code=code).exists():
        code = f"{base}-{suffix}"
        suffix += 1
    return code


@transaction.atomic
def create_fabric_rolls(receipt, roll_weights=None):
    if receipt.rolls.exists():
        return list(receipt.rolls.all())

    weights = [Decimal(str(x)) for x in (roll_weights or [])]
    if len(weights) != int(receipt.roll_count or 0):
        raise ValidationError(
            f"Expected {receipt.roll_count} fabric roll weight(s), got {len(weights)}."
        )

    rolls = []
    for index, weight in enumerate(weights, start=1):
        if weight <= 0:
            raise ValidationError(f"Roll {index} weight must be greater than 0 KG.")

        weight = weight.quantize(Decimal("0.001"))
        roll = FabricRoll.objects.create(
            receipt=receipt,
            roll_code=_fabric_roll_code(receipt, index, weight),
            original_qty=weight,
            remaining_qty=weight,
        )
        rolls.append(roll)
    return rolls


@transaction.atomic
def confirm_cutting(project, user=None):
    project = ProductionProject.objects.select_for_update().prefetch_related("project_colors").get(pk=project.pk)
    if project.status in [ProductionProject.STATUS_SENT, ProductionProject.STATUS_PARTIAL_RETURN, ProductionProject.STATUS_COMPLETED]:
        raise ValidationError("Cutting is already confirmed for this project.")

    colors = list(project.project_colors.select_related("color").all())
    if not colors:
        raise ValidationError("Add at least one colour to the project.")
    if project.cut_total <= 0:
        raise ValidationError("Enter the actual cut quantity by colour and size first.")

    usages = list(
        project.roll_usages.select_related(
            "project_color__color", "roll__receipt", "roll__receipt__color", "roll__receipt__fabric_type"
        ).select_for_update()
    )
    if not usages:
        raise ValidationError("Reserve at least one fabric roll before confirming cutting.")

    usage_color_ids = {u.project_color_id for u in usages if u.project_color_id}
    cut_color_ids = set(project.cut_sizes.filter(cut_qty__gt=0).values_list("project_color_id", flat=True))
    missing = cut_color_ids - usage_color_ids
    if missing:
        names = list(project.project_colors.filter(id__in=missing).values_list("color__name", flat=True))
        raise ValidationError("Reserve fabric rolls for: " + ", ".join(names))

    for usage in usages:
        if usage.applied:
            continue
        if not usage.project_color_id:
            raise ValidationError("A reserved roll is missing its project colour.")
        roll = FabricRoll.objects.select_for_update().select_related("receipt").get(pk=usage.roll_id)
        issued = Decimal(usage.issued_qty or 0)
        returned = Decimal(usage.returned_qty or 0)
        if roll.receipt.color_id != usage.project_color.color_id:
            raise ValidationError(f"{roll.roll_code} has the wrong colour.")
        if project.fabric_type_id and roll.receipt.fabric_type_id != project.fabric_type_id:
            raise ValidationError(f"{roll.roll_code} has the wrong fabric type.")
        if issued > Decimal(roll.remaining_qty or 0):
            raise ValidationError(f"{roll.roll_code} does not have enough remaining quantity.")
        if returned > issued:
            raise ValidationError(f"Returned quantity is invalid for {roll.roll_code}.")

        before = Decimal(roll.remaining_qty or 0)
        after = before - issued + returned
        roll.remaining_qty = after
        roll.save(update_fields=["remaining_qty", "status"])
        usage.roll_qty_before = before
        usage.roll_qty_after = after
        usage.applied = True
        usage.save(update_fields=["roll_qty_before", "roll_qty_after", "applied"])

    project.status = ProductionProject.STATUS_CUT_COMPLETE
    project.save(update_fields=["status", "updated_at"])
    return project


def job_size_summary(job):
    summary = []
    returns = job.returns.exclude(status=SewingReturn.STATUS_CANCELLED)
    for line in job.lines.select_related("size").all():
        totals = returns.filter(lines__size=line.size).aggregate(
            good=Sum("lines__good_qty"),
            damaged=Sum("lines__damaged_qty"),
            missing=Sum("lines__missing_qty"),
        )
        good = int(totals["good"] or 0)
        damaged = int(totals["damaged"] or 0)
        missing = int(totals["missing"] or 0)
        pending = max(int(line.sent_qty or 0) - good - damaged - missing, 0)
        summary.append({
            "size": line.size,
            "sent": int(line.sent_qty or 0),
            "good": good,
            "damaged": damaged,
            "missing": missing,
            "pending": pending,
        })
    return summary


def validate_return_quantities(job, quantities, current_return=None):
    summary = {row["size"].id: row for row in job_size_summary(job)}
    if current_return:
        for line in current_return.lines.all():
            row = summary.get(line.size_id)
            if row:
                row["good"] -= int(line.good_qty or 0)
                row["damaged"] -= int(line.damaged_qty or 0)
                row["missing"] -= int(line.missing_qty or 0)
                row["pending"] += line.total_accounted

    total_entered = 0
    for size_id, values in quantities.items():
        good = int(values.get("good") or 0)
        damaged = int(values.get("damaged") or 0)
        missing = int(values.get("missing") or 0)
        entered = good + damaged + missing
        total_entered += entered
        row = summary.get(int(size_id))
        if row is None:
            raise ValidationError("A selected size was not sent to this sewing partner.")
        if entered > row["pending"]:
            raise ValidationError(
                f"{row['size'].name}: entered {entered}, but only {row['pending']} remain with the sewer."
            )
    if total_entered <= 0:
        raise ValidationError("Enter at least one returned, damaged, or missing piece.")


@transaction.atomic
def sync_sewing_payables(job):
    job = SewingJob.objects.select_for_update().get(pk=job.pk)
    for sewing_return in job.returns.filter(status=SewingReturn.STATUS_STOCKED):
        amount = (Decimal(sewing_return.good_total or 0) * Decimal(job.price_per_piece or 0)).quantize(Decimal("0.01"))
        payable, _ = ProductionPayable.objects.get_or_create(
            sewing_return=sewing_return,
            defaults={
                "payable_type": (ProductionPayable.TYPE_STAFF if job.worker_type == SewingJob.WORKER_STAFF else ProductionPayable.TYPE_SEWER),
                "project": job.project,
                "sewing_job": job,
                "payee_name": job.payee_name,
                "description": f"Sewing return {sewing_return.return_no}",
                "amount": amount,
                "created_by": sewing_return.stocked_by or sewing_return.created_by,
            },
        )
        if payable.paid_amount <= 0:
            payable.amount = amount
            payable.payee_name = job.payee_name
            payable.save(update_fields=["amount", "payee_name"])


@transaction.atomic
def confirm_sewing_return(sewing_return, user=None):
    sewing_return = (
        SewingReturn.objects.select_for_update()
        .select_related("job__project__finished_item", "job__project_color__color", "job__partner")
        .get(pk=sewing_return.pk)
    )
    if sewing_return.status == SewingReturn.STATUS_STOCKED:
        raise ValidationError("This return was already stocked in.")
    if sewing_return.status == SewingReturn.STATUS_CANCELLED:
        raise ValidationError("Cancelled return cannot be stocked in.")

    job = SewingJob.objects.select_for_update().get(pk=sewing_return.job_id)
    quantities = {
        line.size_id: {
            "good": line.good_qty,
            "damaged": line.damaged_qty,
            "missing": line.missing_qty,
        }
        for line in sewing_return.lines.all()
    }
    validate_return_quantities(job, quantities, current_return=sewing_return)

    batch_no = f"PROD-{sewing_return.return_no}"
    batch = InventoryBatch.objects.create(
        batch_no=batch_no,
        supplier=job.payee_name,
        received_date=sewing_return.return_date,
        total_goods_cost=ZERO,
        shipping_cost=ZERO,
        extra_cost=ZERO,
        status=InventoryBatch.STATUS_FINAL,
        note=f"Finished goods from {job.job_no} / {job.project.project_no}",
        created_by=user,
        updated_by=user,
    )

    for line in sewing_return.lines.select_related("size").all():
        good_qty = Decimal(line.good_qty or 0)
        if good_qty <= 0:
            continue
        batch_item = InventoryBatchItem.objects.create(
            batch=batch,
            item=job.project.finished_item,
            color=job.project_color.color if job.project_color_id else job.project.color,
            size=line.size,
            qty_received=good_qty,
            qty_remaining=good_qty,
            base_unit_cost=ZERO,
            final_unit_cost=ZERO,
            is_active=True,
        )
        log_stock_in(
            batch_item=batch_item,
            qty_before=ZERO,
            qty_after=good_qty,
            batch=batch,
            user=user,
            remark=f"Production return {sewing_return.return_no} / {job.project.project_no}",
        )

    sewing_return.status = SewingReturn.STATUS_STOCKED
    sewing_return.stock_batch = batch
    sewing_return.stocked_at = timezone.now()
    sewing_return.stocked_by = user
    sewing_return.save(update_fields=["status", "stock_batch", "stocked_at", "stocked_by"])

    sync_sewing_payables(job)

    pending = job.pending_total
    job.status = SewingJob.STATUS_COMPLETED if pending <= 0 else SewingJob.STATUS_PARTIAL
    job.save(update_fields=["status"])

    project = job.project
    project.status = ProductionProject.STATUS_COMPLETED if project.still_with_sewer <= 0 else ProductionProject.STATUS_PARTIAL_RETURN
    project.save(update_fields=["status", "updated_at"])
    return sewing_return


@transaction.atomic
def pay_selected_payables(payables, form_data, user=None):
    ids = [item.id for item in payables]
    locked = list(ProductionPayable.objects.select_for_update().filter(id__in=ids).select_related("project"))
    if not locked:
        raise ValidationError("Select at least one unpaid project.")

    payable_type = locked[0].payable_type
    payee_name = locked[0].payee_name
    for item in locked:
        if item.payable_type != payable_type:
            raise ValidationError("Sewer and staff payments cannot be mixed.")
        if item.payee_name != payee_name:
            raise ValidationError("Select projects for only one payee at a time.")
        if item.balance <= 0:
            raise ValidationError(f"{item.project.project_no} is already paid.")

    total = sum((item.balance for item in locked), ZERO)
    batch = ProductionPaymentBatch.objects.create(
        payable_type=payable_type,
        payee_name=payee_name,
        payment_date=form_data["payment_date"],
        payment_method=form_data["payment_method"],
        reference=form_data.get("reference") or "",
        note=form_data.get("note") or "",
        total_amount=total,
        created_by=user,
    )

    for item in locked:
        amount = item.balance
        ProductionPaymentAllocation.objects.create(
            payment_batch=batch,
            payable=item,
            amount=amount,
        )
        item.paid_amount = Decimal(item.paid_amount or 0) + amount
        item.save(update_fields=["paid_amount"])

    from finance.models import Expense

    project_numbers = ", ".join(sorted({item.project.project_no for item in locked}))
    type_label = "Sewing" if payable_type == ProductionPayable.TYPE_SEWER else "Production staff"
    expense = Expense.objects.create(
        created_at=timezone.now(),
        created_by=user,
        expense_type=Expense.TYPE_OPERATING,
        amount=total,
        category=Expense.OPERATING_OTHER,
        note=(
            f"{type_label} payment {batch.payment_no} to {payee_name}. "
            f"Projects: {project_numbers}. Method: {batch.get_payment_method_display()}. "
            f"Reference: {batch.reference or '-'}"
        ),
    )
    batch.finance_expense_id = expense.id
    batch.save(update_fields=["finance_expense_id"])
    return batch
