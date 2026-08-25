from collections import OrderedDict
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.http import JsonResponse
from django.utils import timezone

from inventory.models import Size

from .forms import (
    CuttingRollUsageForm,
    FabricReceiptForm,
    FabricReceiptHeaderForm,
    FabricTypeForm,
    fabric_receipt_line_formset,
    PaymentBatchForm,
    ProductionProjectForm,
    SewingJobForm,
    SewingPartnerForm,
    SewingReturnForm,
    StaffPayableForm,
    ProductionSupplierForm,
    ProductionExpenseForm,
)
from .models import (
    CuttingRollUsage,
    CuttingSizeLine,
    FabricReceipt,
    FabricRoll,
    FabricType,
    ProductionPayable,
    ProductionPaymentBatch,
    ProductionPlanSize,
    ProductionProject,
    ProductionProjectColor,
    SewingJob,
    SewingJobLine,
    SewingPartner,
    SewingReturn,
    SewingReturnLine,
    ProductionSupplier,
    ProductionExpense,
)
from .services import (
    confirm_cutting,
    confirm_sewing_return,
    create_fabric_rolls,
    job_size_summary,
    pay_selected_payables,
    sync_sewing_payables,
    validate_return_quantities,
)


def _decimal(value, default=Decimal("0")):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _active_sizes():
    return Size.objects.filter(is_active=True).order_by("sort_order", "id")


def _cutting_is_complete(project):
    """Return cutting completion without requiring a dedicated model field."""
    if project.status in {
        ProductionProject.STATUS_DRAFT,
        ProductionProject.STATUS_CUTTING,
        ProductionProject.STATUS_CANCELLED,
    }:
        return False

    if project.roll_usages.filter(applied=False).exists():
        return False

    return bool(
        int(project.cut_total or 0) > 0
        and project.status in {
            ProductionProject.STATUS_CUT_COMPLETE,
            ProductionProject.STATUS_SENT,
            ProductionProject.STATUS_PARTIAL_RETURN,
            ProductionProject.STATUS_COMPLETED,
        }
    )


def _recalculate_project_status(project):
    """
    Rebuild production status from actual production movement.

    Good, damaged, and missing pieces are all ACCOUNTED FOR.
    Only good pieces go into finished inventory, but damaged/missing pieces
    still close the sewing quantity so production can complete.
    """
    project.refresh_from_db()

    cut_total = int(project.cut_total or 0)
    sent_total = int(project.sent_total or 0)

    stocked_totals = SewingReturnLine.objects.filter(
        sewing_return__job__project=project,
        sewing_return__status=SewingReturn.STATUS_STOCKED,
    ).aggregate(
        good=Sum("good_qty"),
        damaged=Sum("damaged_qty"),
        missing=Sum("missing_qty"),
    )

    good_total = int(stocked_totals["good"] or 0)
    damaged_total = int(stocked_totals["damaged"] or 0)
    missing_total = int(stocked_totals["missing"] or 0)
    accounted_total = good_total + damaged_total + missing_total

    remaining_to_send = max(cut_total - sent_total, 0)
    still_with_sewer = max(sent_total - accounted_total, 0)

    if (
        _cutting_is_complete(project)
        and cut_total > 0
        and remaining_to_send <= 0
        and still_with_sewer <= 0
    ):
        status = ProductionProject.STATUS_COMPLETED
    elif accounted_total > 0:
        status = ProductionProject.STATUS_PARTIAL_RETURN
    elif still_with_sewer > 0 or sent_total > 0:
        status = ProductionProject.STATUS_SENT
    elif _cutting_is_complete(project):
        status = ProductionProject.STATUS_CUT_COMPLETE
    elif cut_total > 0 or project.roll_usages.exists():
        status = ProductionProject.STATUS_CUTTING
    else:
        status = ProductionProject.STATUS_DRAFT

    project.status = status
    project.save(update_fields=["status", "updated_at"])
    return project


@login_required
@permission_required("production.view_production_nav", raise_exception=True)
def dashboard(request):
    """Merged Production landing page: summary + searchable order list."""
    return project_list(request)


@login_required
@permission_required("production.view_fabricroll", raise_exception=True)
def material_stock(request):
    q = (request.GET.get("q") or "").strip()
    rolls = FabricRoll.objects.select_related("receipt", "receipt__color").all()
    if q:
        rolls = rolls.filter(
            Q(roll_code__icontains=q)
            | Q(receipt__fabric_name__icontains=q)
            | Q(receipt__color__name__icontains=q)
            | Q(receipt__supplier__icontains=q)
        )

    available = rolls.filter(remaining_qty__gt=0)
    grouped = OrderedDict()
    for roll in rolls.order_by("receipt__fabric_name", "receipt__color__name", "roll_code"):
        key = (roll.receipt.fabric_name.strip().lower(), roll.receipt.color_id)
        if key not in grouped:
            grouped[key] = {
                "fabric_name": roll.receipt.fabric_name,
                "color": roll.receipt.color,
                "rolls": [],
                "physical_count": 0,
                "full_count": 0,
                "partial_count": 0,
                "equivalent_total": Decimal("0"),
                "remaining_value": Decimal("0"),
            }
        group = grouped[key]
        group["rolls"].append(roll)
        if Decimal(roll.remaining_qty or 0) > 0:
            group["physical_count"] += 1
            group["equivalent_total"] += Decimal(roll.remaining_qty or 0)
            group["remaining_value"] += Decimal(roll.remaining_value or 0)
            if roll.status == FabricRoll.STATUS_FULL:
                group["full_count"] += 1
            elif roll.status == FabricRoll.STATUS_PARTIAL:
                group["partial_count"] += 1

    return render(request, "production/material_stock.html", {
        "roll_groups": list(grouped.values()),
        "q": q,
        "full_count": available.filter(status=FabricRoll.STATUS_FULL).count(),
        "partial_count": available.filter(status=FabricRoll.STATUS_PARTIAL).count(),
        "physical_count": available.count(),
        "equivalent_total": available.aggregate(total=Sum("remaining_qty"))["total"] or 0,
        "remaining_value": sum((r.remaining_value for r in available), Decimal("0")),
        "can_view_cost": request.user.has_perm("production.view_production_cost"),
    })


@login_required
@permission_required("production.add_fabricreceipt", raise_exception=True)
def fabric_receipt_create(request):
    can_view_cost = request.user.has_perm("production.view_production_cost")
    if request.method == "POST":
        header_form = FabricReceiptHeaderForm(request.POST)
        line_formset = fabric_receipt_line_formset(data=request.POST, user=request.user)
        if header_form.is_valid() and line_formset.is_valid():
            total_rolls = 0
            saved_lines = 0
            created_receipts = []
            with transaction.atomic():
                for line_form in line_formset:
                    if not line_form.cleaned_data or line_form.cleaned_data.get("DELETE"):
                        continue
                    data = line_form.cleaned_data
                    receipt = FabricReceipt(
                        received_date=header_form.cleaned_data["received_date"],
                        supplier_ref=header_form.cleaned_data["supplier"],
                        supplier=header_form.cleaned_data["supplier"].name,
                        fabric_type=data["fabric_type"],
                        fabric_name=data["fabric_type"].name,
                        color=data["color"],
                        roll_count=data["roll_count"],
                        total_goods_cost=data.get("total_goods_cost") or Decimal("0"),
                        shipping_cost=data.get("shipping_cost") or Decimal("0"),
                        extra_cost=data.get("extra_cost") or Decimal("0"),
                        note=data.get("note") or "",
                        created_by=request.user,
                        updated_by=request.user,
                    )
                    receipt.save()
                    create_fabric_rolls(receipt, data["roll_weights_list"])
                    created_receipts.append(receipt)
                    total_rolls += receipt.roll_count
                    saved_lines += 1
            messages.success(
                request,
                f"{total_rolls} fabric rolls across {saved_lines} fabric types received successfully.",
            )
            if created_receipts:
                return redirect("production_fabric_receipt_detail", pk=created_receipts[0].pk)
            return redirect("production_material_stock")
    else:
        header_form = FabricReceiptHeaderForm(initial={"received_date": timezone.localdate()})
        line_formset = fabric_receipt_line_formset(user=request.user)

    return render(request, "production/fabric_receipt_form.html", {
        "header_form": header_form,
        "line_formset": line_formset,
        "page_title_text": "Receive Fabric Rolls",
        "can_view_cost": can_view_cost,
        "is_multi_create": True,
    })


@login_required
def fabric_roll_labels(request):
    """Printable 10cm x 10cm labels. Supports all labels or one selected roll."""
    raw_ids = (request.GET.get("receipt_ids") or "").strip()
    single_roll_id = (request.GET.get("roll_id") or "").strip()
    selected_roll_ids_raw = (request.GET.get("roll_ids") or "").strip()

    receipt_ids = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()]
    selected_roll_ids = [
        int(x)
        for x in selected_roll_ids_raw.split(",")
        if x.strip().isdigit()
    ]

    rolls_qs = (
        FabricRoll.objects
        .select_related("receipt", "receipt__color", "receipt__supplier_ref", "receipt__fabric_type")
    )

    if selected_roll_ids:
        # Detail page can send several exact physical rolls at once.
        rolls_qs = rolls_qs.filter(pk__in=selected_roll_ids)
    elif single_roll_id.isdigit():
        rolls_qs = rolls_qs.filter(pk=int(single_roll_id))
    else:
        rolls_qs = rolls_qs.filter(receipt_id__in=receipt_ids)

    rolls_qs = rolls_qs.order_by("receipt_id", "id")

    labels = []
    position_cache = {}
    for roll in rolls_qs:
        if roll.receipt_id not in position_cache:
            ordered_ids = list(
                FabricRoll.objects.filter(receipt_id=roll.receipt_id)
                .order_by("id")
                .values_list("id", flat=True)
            )
            position_cache[roll.receipt_id] = {
                rid: index for index, rid in enumerate(ordered_ids, start=1)
            }
        current = position_cache[roll.receipt_id].get(roll.id, 1)
        labels.append({
            "roll": roll,
            "roll_no": current,
            "roll_no_text": f"{current:02d}",
            "roll_total": int(roll.receipt.roll_count or 0),
            "roll_total_text": f"{int(roll.receipt.roll_count or 0):02d}",
        })

    back_receipt_id = labels[0]["roll"].receipt_id if labels else None
    return render(request, "production/fabric_roll_labels.html", {
        "labels": labels,
        "back_receipt_id": back_receipt_id,
        "single_label": bool(
            single_roll_id.isdigit()
            or len(selected_roll_ids) == 1
        ),
    })


@login_required
@permission_required("production.view_fabricreceipt", raise_exception=True)
def fabric_receipt_detail(request, pk):
    """Stock In detail for one fabric colour batch with print-all / print-one labels."""
    receipt = get_object_or_404(
        FabricReceipt.objects.select_related(
            "supplier_ref", "color", "fabric_type", "created_by", "updated_by"
        ).prefetch_related("rolls"),
        pk=pk,
    )
    rolls = list(receipt.rolls.all().order_by("id"))
    total_original_kg = sum((Decimal(r.original_qty or 0) for r in rolls), Decimal("0"))
    total_remaining_kg = sum((Decimal(r.remaining_qty or 0) for r in rolls), Decimal("0"))
    roll_rows = []
    total = len(rolls)
    for index, roll in enumerate(rolls, start=1):
        roll_rows.append({
            "roll": roll,
            "roll_no_text": f"{index:02d}",
            "roll_total_text": f"{total:02d}",
        })

    return render(request, "production/fabric_receipt_detail.html", {
        "receipt": receipt,
        "roll_rows": roll_rows,
        "total_original_kg": total_original_kg,
        "total_remaining_kg": total_remaining_kg,
    })


@login_required
@permission_required("production.change_fabricreceipt", raise_exception=True)
def fabric_receipt_edit(request, pk):
    receipt = get_object_or_404(FabricReceipt, pk=pk)
    if request.method == "POST":
        form = FabricReceiptForm(request.POST, instance=receipt, user=request.user)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.updated_by = request.user
            obj.save()
            messages.success(request, "Fabric receipt updated.")
            return redirect("production_material_stock")
    else:
        form = FabricReceiptForm(instance=receipt, user=request.user)
    return render(request, "production/fabric_receipt_form.html", {
        "form": form,
        "receipt": receipt,
        "page_title_text": f"Edit {receipt.receipt_no}",
        "can_view_cost": request.user.has_perm("production.view_production_cost"),
        "is_multi_create": False,
    })


@login_required
@permission_required("production.view_fabrictype", raise_exception=True)
def fabric_type_list(request):
    q = (request.GET.get("q") or "").strip()
    rows = FabricType.objects.all().order_by("name")
    if q:
        rows = rows.filter(
            Q(name__icontains=q) | Q(composition__icontains=q) | Q(note__icontains=q)
        )
    return render(request, "production/fabric_type_list.html", {"fabric_types": rows, "q": q})


@login_required
@permission_required("production.add_fabrictype", raise_exception=True)
def fabric_type_create(request):
    if request.method == "POST":
        form = FabricTypeForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Fabric type created.")
            return redirect("production_fabric_type_list")
    else:
        form = FabricTypeForm()
    return render(request, "production/fabric_type_form.html", {
        "form": form,
        "page_title_text": "Create Fabric Type",
        "submit_label": "Save Fabric Type",
    })


@login_required
@permission_required("production.change_fabrictype", raise_exception=True)
def fabric_type_edit(request, pk):
    fabric_type = get_object_or_404(FabricType, pk=pk)
    if request.method == "POST":
        form = FabricTypeForm(request.POST, instance=fabric_type)
        if form.is_valid():
            form.save()
            messages.success(request, "Fabric type updated.")
            return redirect("production_fabric_type_list")
    else:
        form = FabricTypeForm(instance=fabric_type)
    return render(request, "production/fabric_type_form.html", {
        "form": form,
        "fabric_type": fabric_type,
        "page_title_text": f"Edit {fabric_type.name}",
        "submit_label": "Update Fabric Type",
    })


@login_required
@permission_required("production.view_productionproject", raise_exception=True)
def project_list(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    base_projects = (
        ProductionProject.objects
        .select_related("finished_item", "fabric_type", "created_by")
        .prefetch_related("project_colors__color")
    )
    projects = base_projects
    if q:
        projects = projects.filter(
            Q(project_no__icontains=q)
            | Q(finished_item__name__icontains=q)
            | Q(finished_item__code__icontains=q)
            | Q(fabric_type__name__icontains=q)
            | Q(project_colors__color__name__icontains=q)
        ).distinct()
    if status:
        projects = projects.filter(status=status)

    all_projects = base_projects
    active_projects = all_projects.exclude(
        status__in=[ProductionProject.STATUS_COMPLETED, ProductionProject.STATUS_CANCELLED]
    )
    available_rolls = FabricRoll.objects.filter(remaining_qty__gt=0)
    unpaid = [
        payable for payable in ProductionPayable.objects.select_related("project")
        if payable.balance > 0
    ]

    return render(request, "production/project_list.html", {
        "projects": projects,
        "q": q,
        "status": status,
        "status_choices": ProductionProject.STATUS_CHOICES,
        "project_count": all_projects.count(),
        "active_project_count": active_projects.count(),
        "cutting_count": active_projects.filter(
            status__in=[ProductionProject.STATUS_DRAFT, ProductionProject.STATUS_CUTTING]
        ).count(),
        "sewing_count": active_projects.filter(
            status__in=[ProductionProject.STATUS_SENT, ProductionProject.STATUS_PARTIAL_RETURN]
        ).count(),
        "available_roll_count": available_rolls.count(),
        "equivalent_rolls": available_rolls.aggregate(total=Sum("remaining_qty"))["total"] or 0,
        "unpaid_count": len(unpaid),
        "unpaid_total": sum((p.balance for p in unpaid), Decimal("0")),
        "can_view_cost": request.user.has_perm("production.view_production_cost"),
    })


@login_required
@permission_required("production.add_productionproject", raise_exception=True)
def project_create(request):
    sizes = list(_active_sizes())
    selected_color_ids = []
    entered = {}
    if request.method == "POST":
        form = ProductionProjectForm(request.POST)
        selected_color_ids = [int(v) for v in request.POST.getlist("colors") if str(v).isdigit()]
        for color_id in selected_color_ids:
            for size in sizes:
                try:
                    entered[(color_id, size.id)] = max(int(request.POST.get(f"plan_{color_id}_{size.id}") or 0), 0)
                except ValueError:
                    entered[(color_id, size.id)] = 0
        total = sum(entered.values())
        if form.is_valid() and total > 0:
            with transaction.atomic():
                project = form.save(commit=False)
                project.created_by = request.user
                project.status = ProductionProject.STATUS_CUTTING
                project.expected_qty = total
                project.save()
                colors = list(form.cleaned_data["colors"])
                project.color = colors[0]
                project.save(update_fields=["color"])
                for order, color in enumerate(colors):
                    pc = ProductionProjectColor.objects.create(project=project, color=color, sort_order=order)
                    for size in sizes:
                        qty = entered.get((color.id, size.id), 0)
                        if qty > 0:
                            ProductionPlanSize.objects.create(project=project, project_color=pc, size=size, planned_qty=qty)
            messages.success(request, "Production project created.")
            return redirect("production_project_detail", pk=project.pk)
        if total <= 0:
            messages.error(request, "Enter at least one planned quantity under a colour.")
    else:
        form = ProductionProjectForm()
    colors = list(form.fields["colors"].queryset)
    matrix = [{"color": c, "sizes": [{"size": z, "value": entered.get((c.id,z.id),0)} for z in sizes]} for c in colors]
    return render(request, "production/project_form.html", {"form": form, "sizes": sizes, "color_matrix": matrix, "selected_color_ids": selected_color_ids})

@login_required
@permission_required("production.change_productionproject", raise_exception=True)
def project_edit(request, pk):
    project = get_object_or_404(
        ProductionProject.objects.prefetch_related(
            "project_colors__color", "project_colors__plan_sizes__size"
        ),
        pk=pk,
    )

    # Once fabric was consumed or sewing started, quantities must be changed
    # from Processing Detail to keep production and stock history correct.
    if project.roll_usages.filter(applied=True).exists() or project.sewing_jobs.exists():
        messages.error(
            request,
            "This order has already entered processing. Update quantities from Processing Detail instead.",
        )
        return redirect("production_project_detail", pk=project.pk)

    sizes = list(_active_sizes())
    selected_color_ids = list(project.project_colors.values_list("color_id", flat=True))
    entered = {
        (line.project_color.color_id, line.size_id): int(line.planned_qty or 0)
        for line in project.plan_sizes.select_related("project_color", "size")
        if line.project_color_id
    }

    if request.method == "POST":
        form = ProductionProjectForm(request.POST, instance=project)
        selected_color_ids = [
            int(value) for value in request.POST.getlist("colors") if str(value).isdigit()
        ]
        entered = {}
        for color_id in selected_color_ids:
            for size in sizes:
                try:
                    entered[(color_id, size.id)] = max(
                        int(request.POST.get(f"plan_{color_id}_{size.id}") or 0), 0
                    )
                except ValueError:
                    entered[(color_id, size.id)] = 0
        total = sum(entered.values())

        if form.is_valid() and total > 0:
            with transaction.atomic():
                project = form.save(commit=False)
                project.expected_qty = total
                project.save()

                selected_colors = list(form.cleaned_data["colors"])
                selected_ids = {color.id for color in selected_colors}
                existing = {
                    pc.color_id: pc
                    for pc in project.project_colors.select_for_update().all()
                }

                for pc in list(existing.values()):
                    if pc.color_id not in selected_ids:
                        if pc.roll_usages.exists() or pc.sewing_jobs.exists() or pc.cut_sizes.exists():
                            raise ValidationError(
                                f"{pc.color.name} already has processing records and cannot be removed."
                            )
                        pc.delete()

                for order, color in enumerate(selected_colors):
                    pc = existing.get(color.id)
                    if pc is None:
                        pc = ProductionProjectColor.objects.create(
                            project=project, color=color, sort_order=order
                        )
                    elif pc.sort_order != order:
                        pc.sort_order = order
                        pc.save(update_fields=["sort_order"])

                    for size in sizes:
                        qty = entered.get((color.id, size.id), 0)
                        line = ProductionPlanSize.objects.filter(
                            project=project, project_color=pc, size=size
                        ).first()
                        if qty > 0:
                            if line:
                                if line.planned_qty != qty:
                                    line.planned_qty = qty
                                    line.save(update_fields=["planned_qty"])
                            else:
                                ProductionPlanSize.objects.create(
                                    project=project,
                                    project_color=pc,
                                    size=size,
                                    planned_qty=qty,
                                )
                        elif line:
                            line.delete()

                project.color = selected_colors[0]
                project.save(update_fields=["color", "updated_at"])

            messages.success(request, "Production order updated.")
            return redirect("production_project_detail", pk=project.pk)

        if total <= 0:
            messages.error(request, "Enter at least one planned quantity under a colour.")
    else:
        form = ProductionProjectForm(instance=project)

    colors = list(form.fields["colors"].queryset)
    matrix = [
        {
            "color": color,
            "sizes": [
                {"size": size, "value": entered.get((color.id, size.id), 0)}
                for size in sizes
            ],
        }
        for color in colors
    ]
    return render(request, "production/project_form.html", {
        "form": form,
        "sizes": sizes,
        "color_matrix": matrix,
        "selected_color_ids": selected_color_ids,
        "project": project,
        "is_edit": True,
    })


@login_required
@permission_required("production.view_productionproject", raise_exception=True)
def project_detail(request, pk):
    project = get_object_or_404(
        ProductionProject.objects.select_related(
            "finished_item", "fabric_type", "created_by", "color"
        ).prefetch_related(
            "project_colors__color",
            "project_colors__plan_sizes__size",
            "project_colors__cut_sizes__size",
            "roll_usages__roll__receipt__color",
            "sewing_jobs__lines__size",
            "sewing_jobs__returns__lines__size",
        ),
        pk=pk,
    )

    sizes = list(_active_sizes())
    color_rows = []
    project_plan_total = 0
    project_cut_total = 0
    project_sent_total = 0
    project_done_total = 0
    project_accounted_total = 0

    for pc in project.project_colors.select_related("color").all():
        plan = {x.size_id: int(x.planned_qty or 0) for x in pc.plan_sizes.all()}
        cut = {x.size_id: int(x.cut_qty or 0) for x in pc.cut_sizes.all()}
        sent = {
            x["size_id"]: int(x["total"] or 0)
            for x in SewingJobLine.objects.filter(job__project_color=pc)
            .values("size_id")
            .annotate(total=Sum("sent_qty"))
        }
        return_lines = (
            SewingReturnLine.objects.filter(
                sewing_return__job__project_color=pc,
                sewing_return__status=SewingReturn.STATUS_STOCKED,
            )
            .values("size_id")
            .annotate(
                good_total=Sum("good_qty"),
                damaged_total=Sum("damaged_qty"),
                missing_total=Sum("missing_qty"),
            )
        )
        done = {x["size_id"]: int(x["good_total"] or 0) for x in return_lines}
        accounted = {
            x["size_id"]: int(x["good_total"] or 0) + int(x["damaged_total"] or 0) + int(x["missing_total"] or 0)
            for x in return_lines
        }

        rows = []
        for size in sizes:
            planned = plan.get(size.id, 0)
            saved_cut = cut.get(size.id, 0)
            sent_qty = sent.get(size.id, 0)
            done_qty = done.get(size.id, 0)
            accounted_qty = accounted.get(size.id, 0)
            rows.append({
                "size": size,
                "planned": planned,
                "cut": saved_cut if saved_cut else planned,
                "variance": saved_cut - planned,
                "saved_cut": saved_cut,
                "sent": sent_qty,
                "done": done_qty,
                "accounted": accounted_qty,
                "available_to_send": max(saved_cut - sent_qty, 0),
                "waiting_from_sewer": max(sent_qty - accounted_qty, 0),
            })
            project_plan_total += planned
            project_cut_total += saved_cut
            project_sent_total += sent_qty
            project_done_total += done_qty
            project_accounted_total += accounted_qty

        reserved_rolls = list(
            pc.roll_usages.select_related(
                "roll", "roll__receipt", "roll__receipt__color", "roll__receipt__fabric_type"
            ).all()
        )

        available_qs = (
            FabricRoll.objects.select_related("receipt", "receipt__color", "receipt__fabric_type")
            .filter(receipt__color_id=pc.color_id, remaining_qty__gt=0)
            .exclude(cutting_usages__applied=False)
        )
        if project.fabric_type_id:
            available_qs = available_qs.filter(receipt__fabric_type_id=project.fabric_type_id)

        available_rolls = list(available_qs.order_by("status", "remaining_qty", "roll_code"))
        full_available = sum(1 for r in available_rolls if r.status == FabricRoll.STATUS_FULL and Decimal(r.available_qty or 0) >= 1)
        partial_available = sum(1 for r in available_rolls if r.status == FabricRoll.STATUS_PARTIAL or Decimal(r.available_qty or 0) < 1)
        available_equivalent = sum((Decimal(r.available_qty or 0) for r in available_rolls), Decimal("0"))

        color_rows.append({
            "pc": pc,
            "rows": rows,
            "rolls": reserved_rolls,
            "available_rolls": available_rolls,
            "available_count": len(available_rolls),
            "full_available": full_available,
            "partial_available": partial_available,
            "available_equivalent": available_equivalent,
            "reserved_total": sum(
                (Decimal(x.issued_qty or 0) for x in reserved_rolls),
                Decimal("0"),
            ),
            "returned_total": sum(
                (Decimal(x.returned_qty or 0) for x in reserved_rolls),
                Decimal("0"),
            ),
            "has_applied_rolls": any(x.applied for x in reserved_rolls),
            "has_unapplied_rolls": any(not x.applied for x in reserved_rolls),
            "reserved_count": len(reserved_rolls),
            "plan_total": sum(plan.values()),
            "cut_total": sum(cut.values()),
            "variance_total": sum(cut.values()) - sum(plan.values()),
            "sent_total": sum(sent.values()),
            "done_total": sum(done.values()),
            "accounted_total": sum(accounted.values()),
        })

    jobs = (
        project.sewing_jobs.select_related("project_color__color", "partner", "created_by")
        .prefetch_related("lines__size", "returns__lines__size", "returns__created_by", "returns__stocked_by")
    )
    job_rows = [{"job": job, "sizes": job_size_summary(job)} for job in jobs]

    history = []
    history.append({
        "at": project.created_at,
        "title": "Production created",
        "detail": f"Planned {project_plan_total} pcs",
        "user": project.created_by,
        "kind": "created",
    })

    reserved_history = {}
    for usage in project.roll_usages.select_related(
        "project_color__color"
    ).all():
        color_id = usage.project_color_id
        item = reserved_history.setdefault(
            color_id,
            {
                "color": usage.project_color.color.name,
                "count": 0,
                "quantity": Decimal("0"),
                "at": usage.created_at,
            },
        )
        item["count"] += 1
        item["quantity"] += Decimal(usage.issued_qty or 0)
        if usage.created_at > item["at"]:
            item["at"] = usage.created_at

    for item in reserved_history.values():
        history.append({
            "at": item["at"],
            "title": "Fabric reserved",
            "detail": (
                f'{item["color"]}: {item["count"]} roll'
                f'{"s" if item["count"] != 1 else ""} '
                f'({item["quantity"].normalize()} total)'
            ),
            "user": None,
            "kind": "fabric",
        })

    applied_usages = list(project.roll_usages.filter(applied=True).order_by("created_at"))
    if applied_usages:
        history.append({
            "at": max(x.created_at for x in applied_usages),
            "title": "Cutting finished",
            "detail": f"Actual cut {project_cut_total} pcs",
            "user": None,
            "kind": "cut",
        })

    for job in jobs:
        history.append({
            "at": job.created_at,
            "title": "Sent to sewer",
            "detail": f"{job.sent_total} pcs to {job.payee_name}",
            "user": job.created_by,
            "kind": "sent",
        })
        for sewing_return in job.returns.all():
            history.append({
                "at": sewing_return.stocked_at or sewing_return.created_at,
                "title": "Partial sewing received" if job.pending_total > 0 else "Sewing received",
                "detail": f"Good {sewing_return.good_total}, damaged {sewing_return.damaged_total}, missing {sewing_return.missing_total}",
                "user": sewing_return.stocked_by or sewing_return.created_by,
                "kind": "return",
            })

    if project.status == ProductionProject.STATUS_COMPLETED:
        history.append({
            "at": project.updated_at,
            "title": "Production completed",
            "detail": f"{project_done_total} pcs added to inventory",
            "user": None,
            "kind": "complete",
        })

    history.sort(key=lambda item: item["at"], reverse=True)

    remaining_to_cut = max(project_plan_total - project_cut_total, 0)
    remaining_to_send = max(project_cut_total - project_sent_total, 0)
    waiting_from_sewer = max(project_sent_total - project_accounted_total, 0)

    latest_job = jobs.order_by("-created_at", "-id").first()
    latest_stocked_return = (
        SewingReturn.objects
        .select_related("job", "stock_batch")
        .filter(
            job__project=project,
            status=SewingReturn.STATUS_STOCKED,
        )
        .order_by("-stocked_at", "-id")
        .first()
    )

    can_undo_receive = False
    undo_receive_reason = ""
    if latest_stocked_return and latest_stocked_return.stock_batch_id:
        payable = getattr(latest_stocked_return, "sewing_payable", None)
        batch_items = list(
            latest_stocked_return.stock_batch.items.all()
        )
        stock_untouched = all(
            Decimal(item.qty_remaining or 0)
            == Decimal(item.qty_received or 0)
            and not item.adjustments.exists()
            for item in batch_items
        )
        payment_untouched = (
            payable is None
            or Decimal(payable.paid_amount or 0) <= 0
        )
        can_undo_receive = stock_untouched and payment_untouched
        if not stock_untouched:
            undo_receive_reason = "Finished stock was already used or adjusted."
        elif not payment_untouched:
            undo_receive_reason = "The sewing payable was already paid."

    can_undo_send = bool(
        latest_job
        and not latest_job.returns.exists()
        and not latest_job.payables.filter(paid_amount__gt=0).exists()
    )

    return render(request, "production/project_detail.html", {
        "project": project,
        "color_rows": color_rows,
        "sizes": sizes,
        "job_rows": job_rows,
        "partners": SewingPartner.objects.filter(
            is_active=True
        ).order_by("name"),
        "default_partner": SewingPartner.objects.filter(
            is_active=True,
        ).order_by("name").first(),
        "today": timezone.localdate(),
        "history": history,
        "project_plan_total": project_plan_total,
        "project_cut_total": project_cut_total,
        "project_sent_total": project_sent_total,
        "project_done_total": project_done_total,
        "project_accounted_total": project_accounted_total,
        "remaining_to_cut": remaining_to_cut,
        "cut_variance": project_cut_total - project_plan_total,
        "remaining_to_send": remaining_to_send,
        "waiting_from_sewer": waiting_from_sewer,
        "latest_job": latest_job,
        "latest_stocked_return": latest_stocked_return,
        "can_undo_receive": can_undo_receive,
        "undo_receive_reason": undo_receive_reason,
        "can_undo_send": can_undo_send,
        "can_reopen_cutting": _cutting_is_complete(project),
        "can_view_cost": request.user.has_perm("production.view_production_cost"),
    })


@login_required
@permission_required("production.add_cuttingrollusage", raise_exception=True)
def project_add_roll(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)
    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    if _cutting_is_complete(project):
        messages.error(
            request,
            "Cutting is already finished. Reopen cutting before adding more fabric.",
        )
        return redirect("production_project_detail", pk=pk)

    pc = get_object_or_404(
        ProductionProjectColor,
        pk=request.POST.get("project_color_id"),
        project=project,
    )

    exact_roll_id = (request.POST.get("roll_id") or "").strip()
    exact_roll_ids = [
        str(value).strip()
        for value in request.POST.getlist("roll_ids")
        if str(value).strip()
    ]

    try:
        with transaction.atomic():
            qs = (
                FabricRoll.objects.select_for_update()
                .select_related("receipt", "receipt__color", "receipt__fabric_type", "receipt__supplier_ref")
                .filter(
                    receipt__color_id=pc.color_id,
                    remaining_qty__gt=0,
                )
                .exclude(cutting_usages__applied=False)
            )
            if project.fabric_type_id:
                qs = qs.filter(receipt__fabric_type_id=project.fabric_type_id)

            # Multi-select flow: choose several exact physical rolls in one save.
            if exact_roll_ids:
                # Preserve the order selected by the browser while validating that
                # every requested roll is still available.
                available = {
                    str(roll.pk): roll
                    for roll in qs.filter(pk__in=exact_roll_ids)
                }
                missing_ids = [
                    roll_id
                    for roll_id in exact_roll_ids
                    if roll_id not in available
                ]
                if missing_ids:
                    raise ValidationError(
                        "One or more selected fabric rolls are no longer available. "
                        "Please reopen the chooser and select again."
                    )

                candidates = [
                    available[roll_id]
                    for roll_id in exact_roll_ids
                ]

            # Single exact-roll flow remains supported.
            elif exact_roll_id:
                roll = qs.filter(pk=exact_roll_id).first()
                if not roll:
                    raise ValidationError("This fabric roll is no longer available.")
                candidates = [roll]

            else:
                # Backward-compatible automatic selection for older forms.
                try:
                    roll_count = int(request.POST.get("roll_count") or 0)
                except (TypeError, ValueError):
                    roll_count = 0
                if roll_count <= 0:
                    raise ValidationError("Choose at least one fabric roll.")

                selection_order = request.POST.get("selection_order") or "FULL_FIRST"
                ordered_qs = (
                    qs.order_by("remaining_qty", "roll_code")
                    if selection_order == "PARTIAL_FIRST"
                    else qs.order_by("-remaining_qty", "roll_code")
                )
                candidates = list(ordered_qs[:roll_count])
                if len(candidates) < roll_count:
                    raise ValidationError(
                        f"Only {len(candidates)} matching rolls are currently available."
                    )

            for roll in candidates:
                usage = CuttingRollUsage(
                    project=project,
                    project_color=pc,
                    roll=roll,
                    issued_qty=Decimal(roll.available_qty or roll.remaining_qty or 0),
                    returned_qty=Decimal("0"),
                    note=(request.POST.get("note") or "").strip(),
                )
                usage.full_clean()
                usage.save()

            if project.status == ProductionProject.STATUS_DRAFT:
                project.status = ProductionProject.STATUS_CUTTING
                project.save(update_fields=["status", "updated_at"])

        if len(candidates) == 1:
            messages.success(request, f"Fabric {candidates[0].roll_code} selected for {pc.color.name}.")
        else:
            messages.success(request, f"{len(candidates)} roll(s) selected for {pc.color.name}.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required("production.delete_cuttingrollusage", raise_exception=True)
def project_remove_roll(request, pk, usage_id):
    project = get_object_or_404(ProductionProject, pk=pk)
    usage = get_object_or_404(CuttingRollUsage, pk=usage_id, project=project)
    if request.method == "POST" and not usage.applied:
        usage.delete()
        messages.success(request, "Fabric roll removed.")
    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required("production.change_productionproject", raise_exception=True)
def project_save_plan_sizes(request, pk):
    project=get_object_or_404(ProductionProject,pk=pk)
    if request.method != "POST": return redirect("production_project_detail",pk=pk)
    total=0
    with transaction.atomic():
        for pc in project.project_colors.all():
            for size in _active_sizes():
                try: qty=max(int(request.POST.get(f"plan_{pc.id}_{size.id}") or 0),0)
                except ValueError: qty=0
                total += qty
                obj,_=ProductionPlanSize.objects.get_or_create(project=project,project_color=pc,size=size)
                if qty: obj.planned_qty=qty; obj.save(update_fields=["planned_qty"])
                else: obj.delete()
        project.expected_qty=total; project.save(update_fields=["expected_qty","updated_at"])
    messages.success(request,"Planned quantities updated.")
    return redirect("production_project_detail",pk=pk)

@login_required
@permission_required("production.change_cuttingsizeline", raise_exception=True)
def project_save_cut_sizes(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)

    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    if _cutting_is_complete(project):
        messages.error(
            request,
            "Cutting is already finished. Reopen it before recording more cutting.",
        )
        return redirect("production_project_detail", pk=pk)

    if project.status in [
        ProductionProject.STATUS_COMPLETED,
        ProductionProject.STATUS_CANCELLED,
    ]:
        messages.error(
            request,
            "Completed or cancelled production cannot be changed.",
        )
        return redirect("production_project_detail", pk=pk)

    try:
        with transaction.atomic():
            old_total = int(project.cut_total or 0)
            new_total = 0

            for pc in project.project_colors.select_related("color").all():
                for size in _active_sizes():
                    raw = request.POST.get(f"cut_{pc.id}_{size.id}")

                    try:
                        qty = max(int(raw or 0), 0)
                    except (TypeError, ValueError):
                        raise ValidationError(
                            f"Enter a valid cut quantity for "
                            f"{pc.color.name} / {size.name}."
                        )

                    existing = CuttingSizeLine.objects.filter(
                        project=project,
                        project_color=pc,
                        size=size,
                    ).first()

                    old_qty = int(existing.cut_qty or 0) if existing else 0

                    # Once pieces were sent, total cut cannot go below sent.
                    sent_qty = int(
                        SewingJobLine.objects.filter(
                            job__project_color=pc,
                            size=size,
                        ).aggregate(total=Sum("sent_qty"))["total"]
                        or 0
                    )

                    if qty < sent_qty:
                        raise ValidationError(
                            f"{pc.color.name} / {size.name}: actual cut "
                            f"cannot be less than {sent_qty}, because those "
                            "pieces were already sent to the sewer."
                        )

                    if qty > 0:
                        if existing:
                            existing.cut_qty = qty
                            existing.save(update_fields=["cut_qty"])
                        else:
                            CuttingSizeLine.objects.create(
                                project=project,
                                project_color=pc,
                                size=size,
                                cut_qty=qty,
                            )
                    elif existing:
                        existing.delete()

                    new_total += qty

            if new_total <= 0:
                raise ValidationError(
                    "Enter at least one partial cutting quantity."
                )

            if new_total == old_total:
                raise ValidationError(
                    "No cutting quantity changed."
                )

            # Partial cutting stays open. Reserved rolls are not consumed yet.
            # This allows staff to cut some, send some, then continue cutting.
            if project.sewing_jobs.exists():
                if project.status != ProductionProject.STATUS_PARTIAL_RETURN:
                    project.status = ProductionProject.STATUS_SENT
            else:
                project.status = ProductionProject.STATUS_CUTTING

            project.save(update_fields=["status", "updated_at"])

        difference = new_total - old_total

        if difference > 0:
            messages.success(
                request,
                f"Partial cutting saved: +{difference} pcs. "
                "The cut total may be below or above the original plan. "
                "The newly cut pieces can now be sent to the sewer.",
            )
        else:
            messages.success(
                request,
                "Partial cutting quantities updated.",
            )

    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required("production.change_productionproject", raise_exception=True)
def project_confirm_cutting(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)

    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    if _cutting_is_complete(project):
        messages.error(request, "Cutting is already finished.")
        return redirect("production_project_detail", pk=pk)

    if project.status in [
        ProductionProject.STATUS_COMPLETED,
        ProductionProject.STATUS_CANCELLED,
    ]:
        messages.error(
            request,
            "Completed or cancelled production cannot be changed.",
        )
        return redirect("production_project_detail", pk=pk)

    try:
        with transaction.atomic():
            project = ProductionProject.objects.select_for_update().get(
                pk=project.pk
            )

            if int(project.cut_total or 0) <= 0:
                raise ValidationError(
                    "Save at least one partial cutting quantity first."
                )

            usages = list(
                CuttingRollUsage.objects.select_for_update()
                .select_related(
                    "roll",
                    "roll__receipt",
                    "project_color__color",
                )
                .filter(
                    project=project,
                    applied=False,
                )
                .order_by("id")
            )

            if not usages and not project.roll_usages.exists():
                raise ValidationError(
                    "Choose fabric rolls before finishing cutting."
                )

            usages_by_color = {}
            for usage in usages:
                usages_by_color.setdefault(
                    usage.project_color_id,
                    [],
                ).append(usage)

            for pc in project.project_colors.select_related("color").all():
                color_usages = usages_by_color.get(pc.id, [])
                if not color_usages:
                    continue

                raw_remaining = request.POST.get(
                    f"returned_total_{pc.id}",
                    "0",
                )

                try:
                    total_remaining = Decimal(
                        str(raw_remaining or "0")
                    )
                except (InvalidOperation, TypeError, ValueError):
                    raise ValidationError(
                        f"Enter a valid total remaining fabric for "
                        f"{pc.color.name}."
                    )

                total_issued = sum(
                    (
                        Decimal(usage.issued_qty or 0)
                        for usage in color_usages
                    ),
                    Decimal("0"),
                )

                if (
                    total_remaining < 0
                    or total_remaining > total_issued
                ):
                    raise ValidationError(
                        f"{pc.color.name}: remaining fabric must be "
                        f"between 0 and {total_issued.normalize()}."
                    )

                # One total remaining amount across all selected rolls.
                amount_left = total_remaining

                for usage in reversed(color_usages):
                    issued = Decimal(usage.issued_qty or 0)
                    returned = min(issued, amount_left)
                    usage.returned_qty = returned
                    usage.save(update_fields=["returned_qty"])
                    amount_left -= returned

            for usage in usages:
                roll = FabricRoll.objects.select_for_update().get(
                    pk=usage.roll_id
                )
                issued = Decimal(usage.issued_qty or 0)
                returned = Decimal(usage.returned_qty or 0)
                before = Decimal(roll.remaining_qty or 0)

                if issued > before:
                    raise ValidationError(
                        f"{roll.roll_code} does not have enough fabric."
                    )

                after = before - issued + returned
                roll.remaining_qty = after
                roll.save(update_fields=["remaining_qty", "status"])

                usage.roll_qty_before = before
                usage.roll_qty_after = after
                usage.applied = True
                usage.save(
                    update_fields=[
                        "roll_qty_before",
                        "roll_qty_after",
                        "applied",
                    ]
                )

            # Keep sewing status if partial pieces were already sent.
            if project.status == ProductionProject.STATUS_PARTIAL_RETURN:
                new_status = ProductionProject.STATUS_PARTIAL_RETURN
            elif project.sewing_jobs.exists():
                new_status = ProductionProject.STATUS_SENT
            else:
                new_status = ProductionProject.STATUS_CUT_COMPLETE

            project.status = new_status
            project.save(
                update_fields=[
                    "status",
                    "updated_at",
                ]
            )

        messages.success(
            request,
            "Cutting finished. The final cut can be below or above the "
            "plan, and the remaining fabric was returned to stock.",
        )

    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required("production.add_sewingjob", raise_exception=True)
def project_send_sewing_inline(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)

    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    if int(project.cut_total or 0) <= int(project.sent_total or 0):
        messages.error(
            request,
            "Save more partial cutting before sending another sewing batch.",
        )
        return redirect("production_project_detail", pk=pk)

    partner_id = request.POST.get("partner_id")
    partner = None

    if partner_id:
        partner = get_object_or_404(
            SewingPartner,
            pk=partner_id,
            is_active=True,
        )
    else:
        partner = SewingPartner.objects.filter(
            is_active=True,
        ).order_by("name").first()

    if partner is None:
        messages.error(
            request,
            "Create a sewing partner and mark it as the default sewer first.",
        )
        return redirect("production_project_detail", pk=pk)

    sent_date = request.POST.get("sent_date") or timezone.localdate()
    note = (request.POST.get("note") or "").strip()

    try:
        with transaction.atomic():
            grand_total = 0
            created_jobs = 0

            for pc in project.project_colors.select_related("color").all():
                cut = {
                    line.size_id: int(line.cut_qty or 0)
                    for line in pc.cut_sizes.all()
                }
                already_sent = {
                    item["size_id"]: int(item["total"] or 0)
                    for item in SewingJobLine.objects.filter(
                        job__project_color=pc
                    )
                    .values("size_id")
                    .annotate(total=Sum("sent_qty"))
                }

                quantities = {}
                color_total = 0

                for size in _active_sizes():
                    available = max(
                        cut.get(size.id, 0) - already_sent.get(size.id, 0),
                        0,
                    )

                    field_name = f"send_{pc.id}_{size.id}"

                    try:
                        qty = max(
                            int(request.POST.get(field_name) or 0),
                            0,
                        )
                    except (TypeError, ValueError):
                        raise ValidationError(
                            f"Enter a valid send quantity for "
                            f"{pc.color.name} / {size.name}."
                        )

                    if qty > available:
                        raise ValidationError(
                            f"{pc.color.name} / {size.name}: "
                            f"only {available} cut pieces are available."
                        )

                    quantities[size.id] = qty
                    color_total += qty

                # Skip a colour when staff enters zero for every size.
                if color_total <= 0:
                    continue

                job = SewingJob(
                    project=project,
                    project_color=pc,
                    worker_type=SewingJob.WORKER_PARTNER,
                    partner=partner,
                    sent_date=sent_date,
                    expected_return_date=None,
                    price_per_piece=Decimal("0"),
                    note=note,
                    created_by=request.user,
                )
                job.save()

                for size_id, qty in quantities.items():
                    if qty > 0:
                        SewingJobLine.objects.create(
                            job=job,
                            size_id=size_id,
                            sent_qty=qty,
                        )

                grand_total += color_total
                created_jobs += 1

            if grand_total <= 0:
                raise ValidationError(
                    "Enter at least one piece to send."
                )

            project.status = ProductionProject.STATUS_SENT
            project.save(update_fields=["status", "updated_at"])

        messages.success(
            request,
            f"{grand_total} pieces across {created_jobs} colour"
            f"{'s' if created_jobs != 1 else ''} sent to {partner.name}.",
        )

    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("production_project_detail", pk=pk)
    if project.status not in [ProductionProject.STATUS_CUT_COMPLETE, ProductionProject.STATUS_SENT, ProductionProject.STATUS_PARTIAL_RETURN]:
        messages.error(request, "Finish cutting before sending to sewer.")
        return redirect("production_project_detail", pk=pk)

    pc = get_object_or_404(ProductionProjectColor, pk=request.POST.get("project_color_id"), project=project)
    partner = get_object_or_404(SewingPartner, pk=request.POST.get("partner_id"), is_active=True)

    try:
        with transaction.atomic():
            values = {}
            total = 0
            cut = {x.size_id: int(x.cut_qty or 0) for x in pc.cut_sizes.all()}
            already_sent = {
                x["size_id"]: int(x["total"] or 0)
                for x in SewingJobLine.objects.filter(job__project_color=pc)
                .values("size_id").annotate(total=Sum("sent_qty"))
            }
            for size in _active_sizes():
                available = max(cut.get(size.id, 0) - already_sent.get(size.id, 0), 0)
                try:
                    qty = max(int(request.POST.get(f"send_{size.id}") or 0), 0)
                except ValueError:
                    raise ValidationError(f"Enter a valid send quantity for {size.name}.")
                if qty > available:
                    raise ValidationError(f"{size.name}: only {available} cut pieces are available.")
                values[size.id] = qty
                total += qty
            if total <= 0:
                raise ValidationError("Enter at least one piece to send.")

            job = SewingJob(
                project=project,
                project_color=pc,
                worker_type=SewingJob.WORKER_PARTNER,
                partner=partner,
                sent_date=request.POST.get("sent_date") or timezone.localdate(),
                expected_return_date=request.POST.get("expected_return_date") or None,
                price_per_piece=_decimal(request.POST.get("price_per_piece")),
                note=(request.POST.get("note") or "").strip(),
                created_by=request.user,
            )
            job.save()
            for size_id, qty in values.items():
                if qty:
                    SewingJobLine.objects.create(job=job, size_id=size_id, sent_qty=qty)

            project.status = ProductionProject.STATUS_SENT
            project.save(update_fields=["status", "updated_at"])

        messages.success(request, f"{total} pieces sent to {partner.name}.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required("production.add_sewingreturn", raise_exception=True)
def project_receive_selected_jobs(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)

    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    selected_job_ids = [
        int(value)
        for value in request.POST.getlist("selected_jobs")
        if str(value).isdigit()
    ]

    if not selected_job_ids:
        messages.error(request, "Check at least one sewing batch to receive.")
        return redirect("production_project_detail", pk=pk)

    jobs = list(
        SewingJob.objects.select_related(
            "project",
            "project_color__color",
            "partner",
        )
        .prefetch_related("lines__size", "returns__lines")
        .filter(
            project=project,
            pk__in=selected_job_ids,
        )
    )

    if len(jobs) != len(set(selected_job_ids)):
        messages.error(request, "One selected sewing batch was not found.")
        return redirect("production_project_detail", pk=pk)

    try:
        raw_return_date = request.POST.get("return_date")
        if raw_return_date:
            try:
                parsed_return_date = date.fromisoformat(raw_return_date)
            except (TypeError, ValueError):
                raise ValidationError("Enter a valid receive date.")
        else:
            parsed_return_date = timezone.localdate()

        with transaction.atomic():
            total_good = 0
            received_jobs = 0

            for job in jobs:
                summary = job_size_summary(job)
                quantities = {}

                for row in summary:
                    size_id = row["size"].id
                    pending = int(row["pending"] or 0)

                    if pending <= 0:
                        continue

                    try:
                        good = max(
                            int(
                                request.POST.get(
                                    f"good_{job.id}_{size_id}",
                                    "0",
                                )
                                or 0
                            ),
                            0,
                        )
                        damaged = max(
                            int(
                                request.POST.get(
                                    f"damaged_{job.id}_{size_id}",
                                    "0",
                                )
                                or 0
                            ),
                            0,
                        )
                        missing = max(
                            int(
                                request.POST.get(
                                    f"missing_{job.id}_{size_id}",
                                    "0",
                                )
                                or 0
                            ),
                            0,
                        )
                    except (TypeError, ValueError):
                        raise ValidationError(
                            f"Enter valid quantities for "
                            f"{job.job_no} / {row['size'].name}."
                        )

                    quantities[size_id] = {
                        "good": good,
                        "damaged": damaged,
                        "missing": missing,
                    }

                validate_return_quantities(job, quantities)

                job_total = sum(
                    values["good"]
                    for values in quantities.values()
                )

                if (
                    sum(
                        values["good"]
                        + values["damaged"]
                        + values["missing"]
                        for values in quantities.values()
                    )
                    <= 0
                ):
                    raise ValidationError(
                        f"{job.job_no}: enter at least one received quantity."
                    )

                sewing_return = SewingReturn.objects.create(
                    job=job,
                    return_date=parsed_return_date,
                    note=(
                        request.POST.get(f"return_note_{job.id}")
                        or ""
                    ).strip(),
                    created_by=request.user,
                )

                for size_id, values in quantities.items():
                    if sum(values.values()) <= 0:
                        continue

                    SewingReturnLine.objects.create(
                        sewing_return=sewing_return,
                        size_id=size_id,
                        good_qty=values["good"],
                        damaged_qty=values["damaged"],
                        missing_qty=values["missing"],
                    )

                confirm_sewing_return(sewing_return, request.user)
                total_good += job_total
                received_jobs += 1

            _recalculate_project_status(project)

        messages.success(
            request,
            f"{received_jobs} sewing batch"
            f"{'es' if received_jobs != 1 else ''} confirmed. "
            f"{total_good} good pieces added to inventory.",
        )

    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required("production.add_sewingreturn", raise_exception=True)
def project_receive_sewing_inline(request, pk, job_id):
    project = get_object_or_404(ProductionProject, pk=pk)
    job = get_object_or_404(
        SewingJob.objects.select_related("project", "project_color__color", "partner"),
        pk=job_id,
        project=project,
    )
    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    summary = job_size_summary(job)
    quantities = {}
    for row in summary:
        size_id = row["size"].id
        try:
            good = max(int(request.POST.get(f"good_{size_id}") or 0), 0)
            damaged = max(int(request.POST.get(f"damaged_{size_id}") or 0), 0)
            missing = max(int(request.POST.get(f"missing_{size_id}") or 0), 0)
        except ValueError:
            messages.error(request, f"Enter valid quantities for {row['size'].name}.")
            return redirect("production_project_detail", pk=pk)
        quantities[size_id] = {"good": good, "damaged": damaged, "missing": missing}

    try:
        validate_return_quantities(job, quantities)
        with transaction.atomic():
            raw_return_date = request.POST.get("return_date")
            if raw_return_date:
                try:
                    parsed_return_date = date.fromisoformat(raw_return_date)
                except (TypeError, ValueError):
                    raise ValidationError("Enter a valid receive date.")
            else:
                parsed_return_date = timezone.localdate()

            sewing_return = SewingReturn.objects.create(
                job=job,
                return_date=parsed_return_date,
                note=(request.POST.get("return_note") or "").strip(),
                created_by=request.user,
            )
            for size_id, values in quantities.items():
                if sum(values.values()) > 0:
                    SewingReturnLine.objects.create(
                        sewing_return=sewing_return,
                        size_id=size_id,
                        good_qty=values["good"],
                        damaged_qty=values["damaged"],
                        missing_qty=values["missing"],
                    )
            confirm_sewing_return(sewing_return, request.user)
            _recalculate_project_status(project)

        missing_total = sum(v["missing"] for v in quantities.values())
        damaged_total = sum(v["damaged"] for v in quantities.values())
        good_total = sum(v["good"] for v in quantities.values())
        message = f"Sewing completed: {good_total} good"
        if damaged_total:
            message += f", {damaged_total} damaged"
        if missing_total:
            message += f", {missing_total} missing"
        message += ". Good pieces were added to Cloth Inventory."
        messages.success(request, message)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required("production.change_productionproject", raise_exception=True)
def project_update_fabric_remaining(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)

    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    pc = get_object_or_404(
        ProductionProjectColor.objects.select_related("color"),
        pk=request.POST.get("project_color_id"),
        project=project,
    )

    try:
        requested_total = Decimal(
            str(
                request.POST.get(
                    f"correct_returned_total_{pc.id}",
                    "0",
                )
                or "0"
            )
        )
    except (InvalidOperation, TypeError, ValueError):
        messages.error(
            request,
            f"Enter a valid remaining fabric quantity for {pc.color.name}.",
        )
        return redirect("production_project_detail", pk=pk)

    try:
        with transaction.atomic():
            usages = list(
                CuttingRollUsage.objects.select_for_update()
                .select_related("roll")
                .filter(
                    project=project,
                    project_color=pc,
                    applied=True,
                )
                .order_by("id")
            )

            if not usages:
                raise ValidationError(
                    f"{pc.color.name}: cutting has not been confirmed yet."
                )

            total_issued = sum(
                (Decimal(usage.issued_qty or 0) for usage in usages),
                Decimal("0"),
            )

            if requested_total < 0 or requested_total > total_issued:
                raise ValidationError(
                    f"{pc.color.name}: remaining fabric must be between "
                    f"0 and {total_issued.normalize()}."
                )

            # Put the total remaining fabric into the last selected rolls.
            # Example: 5 rolls and total remaining 0.5 means only one roll
            # becomes a 0.5 partial roll.
            new_returns = {}
            amount_left = requested_total

            for usage in reversed(usages):
                issued = Decimal(usage.issued_qty or 0)
                returned = min(issued, amount_left)
                new_returns[usage.id] = returned
                amount_left -= returned

            if amount_left > 0:
                raise ValidationError(
                    "The entered remaining quantity is larger than "
                    "the selected fabric."
                )

            for usage in usages:
                old_returned = Decimal(usage.returned_qty or 0)
                new_returned = new_returns.get(usage.id, Decimal("0"))
                delta = new_returned - old_returned

                if delta == 0:
                    continue

                roll = FabricRoll.objects.select_for_update().get(
                    pk=usage.roll_id
                )
                current_remaining = Decimal(roll.remaining_qty or 0)
                new_roll_remaining = current_remaining + delta

                if new_roll_remaining < 0:
                    raise ValidationError(
                        f"{roll.roll_code} no longer has enough fabric "
                        "to reduce the returned quantity."
                    )

                roll.remaining_qty = new_roll_remaining
                roll.save(update_fields=["remaining_qty", "status"])

                usage.returned_qty = new_returned
                usage.roll_qty_after = (
                    Decimal(usage.roll_qty_after or current_remaining)
                    + delta
                )
                usage.save(
                    update_fields=[
                        "returned_qty",
                        "roll_qty_after",
                    ]
                )

        messages.success(
            request,
            f"{pc.color.name} remaining fabric updated to "
            f"{requested_total.normalize()} roll. Fabric stock was adjusted.",
        )

    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required(
    "production.change_productionproject",
    raise_exception=True,
)
def project_reopen_cutting(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)

    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    if not _cutting_is_complete(project):
        messages.info(request, "Cutting is already open.")
        return redirect("production_project_detail", pk=pk)

    project.status = ProductionProject.STATUS_CUTTING
    project.save(update_fields=["status", "updated_at"])

    messages.success(
        request,
        "Cutting reopened. Existing fabric and stock were not changed. "
        "You can add more rolls and record more cutting.",
    )
    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required(
    "production.delete_sewingjob",
    raise_exception=True,
)
def project_undo_latest_sewing_job(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)

    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    try:
        with transaction.atomic():
            job = (
                SewingJob.objects.select_for_update()
                .filter(project=project)
                .order_by("-created_at", "-id")
                .first()
            )

            if job is None:
                raise ValidationError("There is no sewing batch to undo.")

            if job.returns.exists():
                raise ValidationError(
                    "This sewing batch already has a receive record. "
                    "Undo the latest receive first."
                )

            paid_payables = job.payables.filter(paid_amount__gt=0)
            if paid_payables.exists():
                raise ValidationError(
                    "This sewing batch has already been paid and cannot be undone."
                )

            job_no = job.job_no
            sent_total = int(job.sent_total or 0)

            # Unpaid zero-value or draft payables can be safely removed.
            job.payables.all().delete()
            job.delete()

            _recalculate_project_status(project)

        messages.success(
            request,
            f"Undone: {job_no}. {sent_total} pieces are ready to send again.",
        )

    except (ValidationError, ProtectedError) as exc:
        if isinstance(exc, ValidationError):
            text = " ".join(exc.messages)
        else:
            text = (
                "This sewing batch is linked to another protected record "
                "and cannot be undone."
            )
        messages.error(request, text)

    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required(
    "production.delete_sewingreturn",
    raise_exception=True,
)
def project_undo_latest_sewing_receive(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)

    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    try:
        with transaction.atomic():
            sewing_return = (
                SewingReturn.objects.select_for_update()
                .select_related("job", "stock_batch")
                .filter(
                    job__project=project,
                    status=SewingReturn.STATUS_STOCKED,
                )
                .order_by("-stocked_at", "-id")
                .first()
            )

            if sewing_return is None:
                raise ValidationError(
                    "There is no stocked sewing receive to undo."
                )

            job = SewingJob.objects.select_for_update().get(
                pk=sewing_return.job_id
            )
            stock_batch = sewing_return.stock_batch

            payable = getattr(sewing_return, "sewing_payable", None)
            if (
                payable is not None
                and Decimal(payable.paid_amount or 0) > 0
            ):
                raise ValidationError(
                    "This sewing receive has already been paid and cannot be undone."
                )

            if stock_batch is None:
                raise ValidationError(
                    "The inventory batch for this receive cannot be found."
                )

            batch_items = list(
                stock_batch.items.select_for_update().all()
            )

            for item in batch_items:
                if (
                    Decimal(item.qty_remaining or 0)
                    != Decimal(item.qty_received or 0)
                ):
                    raise ValidationError(
                        "Some finished stock from this receive was already used. "
                        "It cannot be undone."
                    )
                if item.adjustments.exists():
                    raise ValidationError(
                        "Some finished stock from this receive was adjusted. "
                        "It cannot be undone."
                    )

            return_no = sewing_return.return_no
            good_total = int(sewing_return.good_total or 0)

            if payable is not None:
                payable.delete()

            # StockLedger protects batch items, so remove only the ledger entries
            # created for this untouched production batch before deleting it.
            for item in batch_items:
                item.ledger_logs.all().delete()

            # Disconnect first, then remove the inventory batch and receive.
            sewing_return.stock_batch = None
            sewing_return.save(update_fields=["stock_batch"])
            stock_batch.delete()
            sewing_return.delete()

            pending = int(job.pending_total or 0)
            job.status = (
                SewingJob.STATUS_COMPLETED
                if pending <= 0
                else SewingJob.STATUS_SENT
            )
            job.save(update_fields=["status"])

            _recalculate_project_status(project)

        messages.success(
            request,
            f"Undone: {return_no}. {good_total} pieces were removed from "
            "finished stock and returned to the sewer pending quantity.",
        )

    except (ValidationError, ProtectedError) as exc:
        if isinstance(exc, ValidationError):
            text = " ".join(exc.messages)
        else:
            text = (
                "This receive is linked to another protected inventory record "
                "and cannot be undone."
            )
        messages.error(request, text)

    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required("production.view_sewingpartner", raise_exception=True)
def partner_list(request):
    partners = SewingPartner.objects.prefetch_related("sewing_jobs", "expenses").all()
    rows = []
    for partner in partners:
        sent = partner.sewing_jobs.aggregate(total=Sum("lines__sent_qty"))["total"] or 0
        returned = SewingReturnLine.objects.filter(sewing_return__job__partner=partner).aggregate(total=Sum("good_qty"))["total"] or 0
        expense_total = partner.expenses.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        rows.append({"partner": partner, "sent": sent, "returned": returned, "pending": max(sent-returned, 0), "expense_total": expense_total})
    return render(request, "production/partner_list.html", {"rows": rows, "form": SewingPartnerForm()})


@login_required
@permission_required("production.add_sewingpartner", raise_exception=True)
def partner_create(request):
    form = SewingPartnerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"ok": True, "message": "Sewing partner saved successfully.", "partner": {"id": obj.id, "name": obj.name, "phone": obj.phone, "location": obj.location}})
        messages.success(request, "Sewing partner created.")
        return redirect("production_partner_list")
    if request.method == "POST" and request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"ok": False, "message": "Failed to save sewing partner.", "errors": form.errors.get_json_data()}, status=400)
    return render(request, "production/partner_form.html", {"form": form})


@login_required
@permission_required("production.view_sewingpartner", raise_exception=True)
def partner_detail(request, pk):
    partner = get_object_or_404(SewingPartner, pk=pk)
    jobs = partner.sewing_jobs.select_related("project").prefetch_related("lines", "returns__lines")
    expenses = partner.expenses.select_related("project")
    sent = jobs.aggregate(total=Sum("lines__sent_qty"))["total"] or 0
    returned = SewingReturnLine.objects.filter(sewing_return__job__partner=partner).aggregate(total=Sum("good_qty"))["total"] or 0
    return render(request, "production/partner_detail.html", {"partner": partner, "jobs": jobs, "expenses": expenses, "sent": sent, "returned": returned, "pending": max(sent-returned,0), "expense_total": expenses.aggregate(total=Sum("amount"))["total"] or 0})


@login_required
@permission_required("production.view_productionsupplier", raise_exception=True)
def supplier_list(request):
    suppliers = ProductionSupplier.objects.prefetch_related("fabric_receipts", "expenses").all()
    rows=[]
    for supplier in suppliers:
        receipts=supplier.fabric_receipts.all()
        rows.append({"supplier": supplier, "receipts": receipts.count(), "rolls": receipts.aggregate(total=Sum("roll_count"))["total"] or 0, "purchase_total": sum((r.total_cost for r in receipts), Decimal("0")), "expense_total": supplier.expenses.aggregate(total=Sum("amount"))["total"] or 0})
    return render(request, "production/supplier_list.html", {"rows": rows, "form": ProductionSupplierForm()})


@login_required
@permission_required("production.add_productionsupplier", raise_exception=True)
def supplier_create(request):
    form=ProductionSupplierForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        obj=form.save()
        if request.headers.get("x-requested-with")=="XMLHttpRequest":
            return JsonResponse({"ok":True,"message":"Supplier saved successfully.","supplier":{"id":obj.id,"name":obj.name,"phone":obj.phone,"location":obj.location}})
        return redirect("production_supplier_list")
    if request.method=="POST" and request.headers.get("x-requested-with")=="XMLHttpRequest":
        return JsonResponse({"ok":False,"message":"Failed to save supplier.","errors":form.errors.get_json_data()},status=400)
    return render(request,"production/supplier_form.html",{"form":form})


@login_required
@permission_required("production.view_productionsupplier", raise_exception=True)
def supplier_detail(request, pk):
    supplier=get_object_or_404(ProductionSupplier,pk=pk)
    receipts=supplier.fabric_receipts.prefetch_related("rolls").all()
    expenses=supplier.expenses.select_related("project")
    return render(request,"production/supplier_detail.html",{"supplier":supplier,"receipts":receipts,"expenses":expenses,"roll_count":receipts.aggregate(total=Sum("roll_count"))["total"] or 0,"purchase_total":sum((r.total_cost for r in receipts),Decimal("0")),"expense_total":expenses.aggregate(total=Sum("amount"))["total"] or 0})


def _sync_finance_expense(production_expense, user):
    from finance.models import Expense
    category = Expense.OPERATING_COMMISSION if production_expense.category == ProductionExpense.CATEGORY_STAFF_COMMISSION else Expense.OPERATING_OTHER
    title = production_expense.get_category_display()
    if production_expense.supplier_id: title += f" - {production_expense.supplier.name}"
    if production_expense.sewing_partner_id: title += f" - {production_expense.sewing_partner.name}"
    finance = Expense.objects.create(expense_type=Expense.TYPE_OPERATING, category=category, amount=production_expense.amount, note=f"{title}. {production_expense.note}".strip(), created_by=user)
    production_expense.finance_expense_id=finance.id
    production_expense.save(update_fields=["finance_expense_id"] )


@login_required
@permission_required("production.view_production_expense", raise_exception=True)
def production_expense_list(request):
    expenses=ProductionExpense.objects.select_related("supplier","sewing_partner","project","created_by")
    return render(request,"production/expense_list.html",{"expenses":expenses,"total":expenses.aggregate(total=Sum("amount"))["total"] or 0,"form":ProductionExpenseForm()})


@login_required
@permission_required("production.add_productionexpense", raise_exception=True)
def production_expense_create(request):
    form=ProductionExpenseForm(request.POST or None)
    if request.method=="POST" and form.is_valid():
        try:
            with transaction.atomic():
                obj=form.save(commit=False); obj.created_by=request.user; obj.full_clean(); obj.save(); _sync_finance_expense(obj,request.user)
            if request.headers.get("x-requested-with")=="XMLHttpRequest":
                return JsonResponse({"ok":True,"message":"Expense saved successfully.","expense":{"id":obj.id,"date":obj.expense_date.strftime("%Y-%m-%d"),"category":obj.get_category_display(),"amount":f"{obj.amount:.2f}"}})
            return redirect("production_expense_list")
        except ValidationError as exc:
            if request.headers.get("x-requested-with")=="XMLHttpRequest": return JsonResponse({"ok":False,"message":"Failed to save expense.","errors":getattr(exc,"message_dict",{"__all__":exc.messages})},status=400)
    if request.method=="POST" and request.headers.get("x-requested-with")=="XMLHttpRequest":
        return JsonResponse({"ok":False,"message":"Failed to save expense.","errors":form.errors.get_json_data()},status=400)
    return render(request,"production/expense_form.html",{"form":form})


@login_required
@permission_required("production.add_sewingjob", raise_exception=True)
def sewing_job_create(request, project_id):
    project=get_object_or_404(ProductionProject,pk=project_id)
    if project.status not in [ProductionProject.STATUS_CUT_COMPLETE,ProductionProject.STATUS_SENT,ProductionProject.STATUS_PARTIAL_RETURN]:
        messages.error(request,"Confirm cutting before sending pieces to sewing.")
        return redirect("production_project_detail",pk=project_id)
    form=SewingJobForm(request.POST or None,user=request.user,project=project)
    selected_pc_id=request.POST.get("project_color") or request.GET.get("color")
    pc=project.project_colors.filter(pk=selected_pc_id).first() or project.project_colors.first()
    rows=[]; entered={}
    if pc:
        cut={x.size_id:int(x.cut_qty or 0) for x in pc.cut_sizes.all()}
        sent={x["size_id"]:int(x["total"] or 0) for x in SewingJobLine.objects.filter(job__project_color=pc).values("size_id").annotate(total=Sum("sent_qty"))}
        for size in _active_sizes():
            available=max(cut.get(size.id,0)-sent.get(size.id,0),0)
            if available or request.method=="POST":
                try: val=max(int(request.POST.get(f"sent_{size.id}") or 0),0)
                except ValueError: val=0
                entered[size.id]=val; rows.append({"size":size,"available":available,"value":val})
    if request.method=="POST" and form.is_valid():
        pc=form.cleaned_data["project_color"]
        cut={x.size_id:int(x.cut_qty or 0) for x in pc.cut_sizes.all()}
        sent={x["size_id"]:int(x["total"] or 0) for x in SewingJobLine.objects.filter(job__project_color=pc).values("size_id").annotate(total=Sum("sent_qty"))}
        errors=[]; total=0; values={}
        for size in _active_sizes():
            try: qty=max(int(request.POST.get(f"sent_{size.id}") or 0),0)
            except ValueError: qty=0
            available=max(cut.get(size.id,0)-sent.get(size.id,0),0)
            if qty>available: errors.append(f"{size.name}: only {available} available.")
            values[size.id]=qty; total+=qty
        if total<=0: errors.append("Enter at least one piece.")
        if not errors:
            with transaction.atomic():
                job=form.save(commit=False); job.project=project; job.created_by=request.user
                if "price_per_piece" not in form.cleaned_data: job.price_per_piece=Decimal("0")
                job.save()
                for sid,qty in values.items():
                    if qty: SewingJobLine.objects.create(job=job,size_id=sid,sent_qty=qty)
                project.status=ProductionProject.STATUS_SENT; project.save(update_fields=["status","updated_at"])
            messages.success(request,"Pieces sent to sewing.")
            return redirect("production_project_detail",pk=project.pk)
        for e in errors: messages.error(request,e)
    return render(request,"production/sewing_job_form.html",{"form":form,"project":project,"rows":rows,"selected_pc":pc,"can_view_cost":request.user.has_perm("production.view_production_cost")})

@login_required
@permission_required("production.change_sewingjob", raise_exception=True)
def sewing_job_edit(request, pk):
    job=get_object_or_404(SewingJob.objects.select_related("project","project_color__color","partner"),pk=pk)
    form=SewingJobForm(request.POST or None,instance=job,user=request.user,project=job.project)
    if request.method=="POST" and form.is_valid():
        old=job.price_per_piece; job=form.save(commit=False)
        if "price_per_piece" not in form.cleaned_data: job.price_per_piece=old
        job.save(); sync_sewing_payables(job)
        messages.success(request,"Sewing job updated.")
        return redirect("production_project_detail",pk=job.project_id)
    return render(request,"production/sewing_job_form.html",{"form":form,"project":job.project,"job":job,"rows":[],"selected_pc":job.project_color,"can_view_cost":request.user.has_perm("production.view_production_cost")})

@login_required
@permission_required("production.add_sewingreturn", raise_exception=True)
def sewing_return_create(request, job_id):
    job = get_object_or_404(SewingJob.objects.select_related("project", "project_color__color", "partner"), pk=job_id)
    summary = job_size_summary(job)
    if request.method == "POST":
        form = SewingReturnForm(request.POST)
        quantities = {}
        for row in summary:
            size_id = row["size"].id
            quantities[size_id] = {
                "good": max(int(request.POST.get(f"good_{size_id}") or 0), 0),
                "damaged": max(int(request.POST.get(f"damaged_{size_id}") or 0), 0),
                "missing": max(int(request.POST.get(f"missing_{size_id}") or 0), 0),
            }
        if form.is_valid():
            try:
                validate_return_quantities(job, quantities)
            except ValidationError as exc:
                for error in exc.messages:
                    messages.error(request, error)
            else:
                with transaction.atomic():
                    sewing_return = form.save(commit=False)
                    sewing_return.job = job
                    sewing_return.created_by = request.user
                    sewing_return.save()
                    for size_id, values in quantities.items():
                        if sum(values.values()) > 0:
                            SewingReturnLine.objects.create(
                                sewing_return=sewing_return,
                                size_id=size_id,
                                good_qty=values["good"],
                                damaged_qty=values["damaged"],
                                missing_qty=values["missing"],
                            )
                messages.success(request, "Partial sewing return recorded. Review and confirm stock in.")
                return redirect("production_return_detail", pk=sewing_return.pk)
    else:
        form = SewingReturnForm()
    return render(request, "production/sewing_return_form.html", {
        "form": form,
        "job": job,
        "summary": summary,
    })


@login_required
@permission_required("production.view_sewingreturn", raise_exception=True)
def sewing_return_list(request):
    """Show every project that has cloth sent to sewing, grouped by project."""
    status_filter = (request.GET.get("status") or "all").strip().lower()
    date_type = (request.GET.get("date_type") or "sent").strip().lower()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    if status_filter not in {"all", "unpaid", "paid", "sent", "received"}:
        status_filter = "all"

    # First decide which projects match the requested date range.
    filtered_jobs = (
        SewingJob.objects
        .exclude(status=SewingJob.STATUS_CANCELLED)
        .select_related("project")
    )

    if date_type == "received":
        if date_from:
            filtered_jobs = filtered_jobs.filter(returns__return_date__gte=date_from)
        if date_to:
            filtered_jobs = filtered_jobs.filter(returns__return_date__lte=date_to)
        filtered_jobs = filtered_jobs.distinct()
    else:
        date_type = "sent"
        if date_from:
            filtered_jobs = filtered_jobs.filter(sent_date__gte=date_from)
        if date_to:
            filtered_jobs = filtered_jobs.filter(sent_date__lte=date_to)

    project_ids = list(
        filtered_jobs.values_list("project_id", flat=True).distinct()
    )

    # Once a project matches the date filter, load all of its colour sewing jobs
    # so project totals stay correct.
    jobs = (
        SewingJob.objects
        .filter(project_id__in=project_ids)
        .exclude(status=SewingJob.STATUS_CANCELLED)
        .select_related(
            "project",
            "project__finished_item",
            "project_color__color",
            "partner",
        )
        .prefetch_related(
            "lines",
            "returns__lines",
            "returns__sewing_payable",
        )
        .order_by(
            "-project__created_at",
            "-project_id",
            "project_color__sort_order",
            "id",
        )
    )

    grouped = OrderedDict()
    total_sent = 0
    total_received = 0
    total_in_sewing = 0
    total_unpaid_good = 0

    for job in jobs:
        project = job.project
        stocked_returns = [
            item for item in job.returns.all()
            if item.status == SewingReturn.STATUS_STOCKED
        ]

        sent_total = int(job.sent_total or 0)
        received_total = sum(
            int(item.good_total or 0)
            for item in stocked_returns
        )
        # pending_total subtracts good + damaged + missing, so a missing/damaged
        # piece can still complete the sewing job correctly.
        remaining = int(job.pending_total or 0)
        latest_received = max(
            (item.return_date for item in stocked_returns),
            default=None,
        )

        unpaid_good = 0
        paid_amount = Decimal("0")
        has_unpaid = False
        has_paid = False

        for sewing_return in stocked_returns:
            good_qty = int(sewing_return.good_total or 0)
            payable = getattr(sewing_return, "sewing_payable", None)

            if payable is None:
                if good_qty > 0:
                    has_unpaid = True
                    unpaid_good += good_qty
                continue

            paid = Decimal(payable.paid_amount or 0)
            paid_amount += paid
            if paid > 0:
                has_paid = True
            if payable.balance > 0:
                has_unpaid = True
                unpaid_good += good_qty

        row = grouped.setdefault(
            project.id,
            {
                "project": project,
                "children": [],
                "payee_name": "",
                "sent_total": 0,
                "received_total": 0,
                "remaining_total": 0,
                "unpaid_good": 0,
                "paid_amount": Decimal("0"),
                "latest_sent": None,
                "latest_received": None,
                "has_unpaid": False,
                "has_paid": False,
                "is_paid": False,
                "is_finished": False,
                "can_pay": False,
                "payment_lock_reason": "",
            },
        )

        row["children"].append({
            "job": job,
            "color": job.project_color.color if job.project_color_id else project.color,
            "sent_total": sent_total,
            "received_total": received_total,
            "remaining": remaining,
            "latest_received": latest_received,
            "unpaid_good": unpaid_good,
            "paid_amount": paid_amount,
            "has_unpaid": has_unpaid,
            "is_paid": bool(stocked_returns) and not has_unpaid and has_paid,
        })

        if not row["payee_name"]:
            row["payee_name"] = job.payee_name
        elif row["payee_name"] != job.payee_name:
            row["payee_name"] = "Multiple sewers"

        row["sent_total"] += sent_total
        row["received_total"] += received_total
        row["remaining_total"] += remaining
        row["unpaid_good"] += unpaid_good
        row["paid_amount"] += paid_amount
        row["has_unpaid"] = row["has_unpaid"] or has_unpaid
        row["has_paid"] = row["has_paid"] or has_paid

        if row["latest_sent"] is None or job.sent_date > row["latest_sent"]:
            row["latest_sent"] = job.sent_date
        if latest_received and (
            row["latest_received"] is None
            or latest_received > row["latest_received"]
        ):
            row["latest_received"] = latest_received

    # Summary is based on all projects matching the date range.
    for row in grouped.values():
        total_sent += row["sent_total"]
        total_received += row["received_total"]
        total_in_sewing += row["remaining_total"]
        total_unpaid_good += row["unpaid_good"]

    project_rows = list(grouped.values())

    for row in project_rows:
        # Fully received/accounted means nothing is left with the sewer.
        row["is_finished"] = bool(
            row["sent_total"] > 0 and row["remaining_total"] == 0
        )
        row["is_paid"] = bool(
            row["is_finished"]
            and row["received_total"] > 0
            and not row["has_unpaid"]
            and row["has_paid"]
        )
        row["can_pay"] = bool(
            row["is_finished"]
            and row["has_unpaid"]
            and row["unpaid_good"] > 0
            and row["payee_name"] != "Multiple sewers"
        )

        if row["is_paid"]:
            row["payment_lock_reason"] = "Project already paid."
        elif row["remaining_total"] > 0:
            row["payment_lock_reason"] = f"{row['remaining_total']} pcs are still with the sewer."
        elif row["payee_name"] == "Multiple sewers":
            row["payment_lock_reason"] = "This project contains multiple sewers."
        elif not row["has_unpaid"]:
            row["payment_lock_reason"] = "No unpaid received cloth."
        elif row["unpaid_good"] <= 0:
            row["payment_lock_reason"] = "No good received cloth is ready to pay."
        else:
            row["payment_lock_reason"] = ""

    if status_filter == "paid":
        project_rows = [row for row in project_rows if row["is_paid"]]
    elif status_filter == "unpaid":
        project_rows = [
            row for row in project_rows
            if row["has_unpaid"] and not row["is_paid"]
        ]
    elif status_filter == "sent":
        project_rows = [
            row for row in project_rows
            if row["sent_total"] > 0 and row["remaining_total"] > 0
        ]
    elif status_filter == "received":
        project_rows = [
            row for row in project_rows
            if row["sent_total"] > 0 and row["remaining_total"] == 0
        ]

    project_rows.sort(
        key=lambda row: (row["latest_sent"] or date.min, row["project"].id),
        reverse=True,
    )

    return render(request, "production/sewing_return_list.html", {
        "project_rows": project_rows,
        "total_projects": len(grouped),
        "total_sent": total_sent,
        "total_received": total_received,
        "total_in_sewing": total_in_sewing,
        "total_unpaid_good": total_unpaid_good,
        "status_filter": status_filter,
        "date_type": date_type,
        "date_from": date_from,
        "date_to": date_to,
        # The current code uses the separate Sewing Payments workflow.
        "payment_form": None,
    })


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
def sewing_return_bulk_pay(request):
    """
    Compatibility view for the existing production_return_bulk_pay URL.

    The current production package uses the regular payment workflow rather than
    the older SewingReturnBulkPaymentForm/pay_selected_sewing_jobs implementation.
    Keep this URL available so Django can load production.urls safely without
    breaking the rest of Production/Fabric features.
    """
    if request.method != "POST":
        return redirect("production_return_list")

    messages.info(
        request,
        "Use the Sewing Payments page to record sewing payments.",
    )
    return redirect("production_return_list")


@login_required
@permission_required("production.view_sewingreturn", raise_exception=True)
def sewing_return_detail(request, pk):
    sewing_return = get_object_or_404(
        SewingReturn.objects.select_related("job__project__finished_item", "job__project__color", "job__partner", "created_by", "stocked_by", "stock_batch"),
        pk=pk,
    )
    return render(request, "production/sewing_return_detail.html", {
        "sewing_return": sewing_return,
        "lines": sewing_return.lines.select_related("size"),
    })


@login_required
@permission_required("production.change_sewingreturn", raise_exception=True)
def sewing_return_confirm(request, pk):
    sewing_return = get_object_or_404(SewingReturn, pk=pk)
    if request.method == "POST":
        try:
            confirm_sewing_return(sewing_return, request.user)
            _recalculate_project_status(sewing_return.job.project)
            messages.success(
                request,
                "Return confirmed. Good pieces were stocked; damaged/missing pieces were recorded and count as completed production."
            )
        except ValidationError as exc:
            for error in exc.messages:
                messages.error(request, error)
    return redirect("production_return_detail", pk=pk)


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
def staff_payable_create(request, project_id):
    project = get_object_or_404(ProductionProject, pk=project_id)
    if request.method == "POST":
        form = StaffPayableForm(request.POST)
        if form.is_valid():
            payable = form.save(commit=False)
            payable.payable_type = ProductionPayable.TYPE_STAFF
            payable.project = project
            payable.created_by = request.user
            payable.save()
            messages.success(request, "Staff payable added.")
        else:
            for error in form.errors.values():
                messages.error(request, " ".join(error))
    return redirect("production_project_detail", pk=project_id)


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
def payment_list(request, payable_type):
    payable_type = payable_type.upper()
    if payable_type not in [ProductionPayable.TYPE_SEWER, ProductionPayable.TYPE_STAFF]:
        payable_type = ProductionPayable.TYPE_SEWER
    all_payables = ProductionPayable.objects.filter(payable_type=payable_type).select_related("project", "sewing_job")
    payables = [item for item in all_payables if item.balance > 0]
    form = PaymentBatchForm(request.POST or None)
    if request.method == "POST":
        selected_ids = request.POST.getlist("selected")
        selected = [item for item in payables if str(item.id) in selected_ids]
        if form.is_valid():
            try:
                batch = pay_selected_payables(selected, form.cleaned_data, request.user)
                messages.success(request, f"{batch.payment_no} paid successfully: ${batch.total_amount}.")
                return redirect("production_payment_list", payable_type=payable_type.lower())
            except ValidationError as exc:
                for error in exc.messages:
                    messages.error(request, error)
    history = ProductionPaymentBatch.objects.filter(payable_type=payable_type).prefetch_related("allocations__payable__project")[:30]
    return render(request, "production/payment_list.html", {
        "payable_type": payable_type,
        "payables": payables,
        "history": history,
        "form": form,
        "title_text": "Sewing Payments" if payable_type == ProductionPayable.TYPE_SEWER else "Staff Payments",
    })