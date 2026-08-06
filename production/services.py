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


@transaction.atomic
def create_fabric_rolls(receipt):
    if receipt.rolls.exists():
        return list(receipt.rolls.all())
    rolls = []
    color_code = (receipt.color.code or "COL").upper()
    for index in range(1, receipt.roll_count + 1):
        code = f"{receipt.receipt_no}-{color_code}-{index:03d}"
        roll = FabricRoll.objects.create(
            receipt=receipt,
            roll_code=code,
            original_qty=Decimal("1.000"),
            remaining_qty=Decimal("1.000"),
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

@transaction.atomic
def pay_selected_sewing_jobs(
    job_ids,
    *,
    price_per_piece,
    payment_date,
    note="",
    user=None,
):
    job_ids = list(dict.fromkeys(int(value) for value in job_ids))
    if not job_ids:
        raise ValidationError("Tick at least one sewing row to pay.")

    jobs = list(
        SewingJob.objects.select_for_update()
        .filter(id__in=job_ids)
        .select_related("partner", "project")
        .prefetch_related("returns__lines", "returns__sewing_payable")
    )
    if len(jobs) != len(job_ids):
        raise ValidationError("One or more selected sewing records could not be found.")

    payee_names = {job.payee_name for job in jobs}
    if len(payee_names) != 1:
        raise ValidationError("Tick records for only one sewer at a time.")

    price = Decimal(price_per_piece or 0)
    if price <= 0:
        raise ValidationError("Enter a price per cloth greater than zero.")

    payables = []
    for job in jobs:
        for sewing_return in job.returns.all():
            if sewing_return.status != SewingReturn.STATUS_STOCKED:
                continue
            payable = getattr(sewing_return, "sewing_payable", None)
            if payable is None:
                continue

            if Decimal(payable.paid_amount or 0) > 0:
                raise ValidationError(
                    f"{sewing_return.return_no} was already paid or partially paid "
                    "and cannot use bulk price payment."
                )

            amount = (Decimal(sewing_return.good_total or 0) * price).quantize(Decimal("0.01"))
            if amount <= 0:
                continue
            payable.amount = amount
            payable.payee_name = job.payee_name
            payable.description = (
                f"Sewing return {sewing_return.return_no}: "
                f"{sewing_return.good_total} pcs × ${price}"
            )
            payable.save(update_fields=["amount", "payee_name", "description"])
            payables.append(payable)

    if not payables:
        raise ValidationError("The selected sewing records have no unpaid received cloth.")

    return pay_selected_payables(
        payables,
        {
            "payment_date": payment_date,
            "payment_method": ProductionPaymentBatch.METHOD_CASH,
            "reference": "",
            "note": note,
        },
        user,
    )


@transaction.atomic
def pay_sewing_project(
    project_id,
    *,
    price_per_piece,
    payment_date,
    note="",
    user=None,
):
    """
    Pay every unpaid stocked sewing return for one completed production project.

    Receiving stock never marks a payable as paid. This function is the only
    action that prices the received cloth and records the sewer payment.
    """
    project = (
        ProductionProject.objects.select_for_update()
        .get(pk=int(project_id))
    )

    if not project.cutting_is_complete:
        raise ValidationError(
            "Finish cutting before paying this production project."
        )

    if int(project.still_with_sewer or 0) > 0:
        raise ValidationError(
            f"{project.project_no} still has "
            f"{project.still_with_sewer} pieces with the sewer."
        )

    price = Decimal(price_per_piece or 0)
    if price <= 0:
        raise ValidationError(
            "Enter a price per cloth greater than zero."
        )

    returns = list(
        SewingReturn.objects.select_for_update()
        .filter(
            job__project=project,
            status=SewingReturn.STATUS_STOCKED,
        )
        .select_related(
            "job",
            "job__partner",
            "sewing_payable",
        )
        .order_by("return_date", "id")
    )

    if not returns:
        raise ValidationError(
            "This project has no confirmed sewing returns to pay."
        )

    payee_names = {
        sewing_return.job.payee_name
        for sewing_return in returns
    }
    if len(payee_names) != 1:
        raise ValidationError(
            "This project contains more than one sewer. "
            "Correct the sewing records before payment."
        )

    payables = []
    unpaid_good_total = 0

    for sewing_return in returns:
        payable = getattr(sewing_return, "sewing_payable", None)

        if payable is None:
            payable = ProductionPayable.objects.create(
                payable_type=ProductionPayable.TYPE_SEWER,
                project=project,
                sewing_job=sewing_return.job,
                sewing_return=sewing_return,
                payee_name=sewing_return.job.payee_name,
                work_type="Sewing",
                description=(
                    f"Sewing return {sewing_return.return_no}"
                ),
                amount=Decimal("0"),
                paid_amount=Decimal("0"),
                created_by=user,
            )

        paid_amount = Decimal(payable.paid_amount or 0)
        if paid_amount > 0:
            # Already-paid returns are not charged again.
            continue

        good_qty = int(sewing_return.good_total or 0)
        if good_qty <= 0:
            continue

        amount = (
            Decimal(good_qty) * price
        ).quantize(Decimal("0.01"))

        payable.amount = amount
        payable.payee_name = sewing_return.job.payee_name
        payable.description = (
            f"{project.project_no} · "
            f"{sewing_return.return_no}: "
            f"{good_qty} pcs × ${price}"
        )
        payable.save(
            update_fields=[
                "amount",
                "payee_name",
                "description",
            ]
        )

        payables.append(payable)
        unpaid_good_total += good_qty

    if not payables:
        raise ValidationError(
            "This production project is already paid."
        )

    payment_note = (
        f"Full project sewing payment for {project.project_no}. "
        f"{unpaid_good_total} received pcs × ${price}."
    )
    if note:
        payment_note = f"{payment_note} {note}".strip()

    return pay_selected_payables(
        payables,
        {
            "payment_date": payment_date,
            "payment_method": ProductionPaymentBatch.METHOD_CASH,
            "reference": project.project_no,
            "note": payment_note,
        },
        user,
    )