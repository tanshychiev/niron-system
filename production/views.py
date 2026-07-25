from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from inventory.models import Size

from .forms import (
    CuttingRollUsageForm,
    FabricReceiptForm,
    FabricReceiptHeaderForm,
    fabric_receipt_line_formset,
    PaymentBatchForm,
    ProductionProjectForm,
    SewingJobForm,
    SewingPartnerForm,
    SewingReturnForm,
    StaffPayableForm,
)
from .models import (
    CuttingRollUsage,
    CuttingSizeLine,
    FabricReceipt,
    FabricRoll,
    ProductionPayable,
    ProductionPaymentBatch,
    ProductionProject,
    SewingJob,
    SewingJobLine,
    SewingPartner,
    SewingReturn,
    SewingReturnLine,
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


@login_required
@permission_required("production.view_production_nav", raise_exception=True)
def dashboard(request):
    projects = ProductionProject.objects.select_related("finished_item", "color").all()
    available_rolls = FabricRoll.objects.filter(remaining_qty__gt=0)
    unpaid = [p for p in ProductionPayable.objects.select_related("project").all() if p.balance > 0]
    return render(request, "production/dashboard.html", {
        "projects": projects[:8],
        "project_count": projects.count(),
        "active_project_count": projects.exclude(status__in=[ProductionProject.STATUS_COMPLETED, ProductionProject.STATUS_CANCELLED]).count(),
        "available_roll_count": available_rolls.count(),
        "equivalent_rolls": available_rolls.aggregate(total=Sum("remaining_qty"))["total"] or 0,
        "unpaid_count": len(unpaid),
        "unpaid_total": sum((p.balance for p in unpaid), Decimal("0")),
        "can_view_cost": request.user.has_perm("production.view_production_cost"),
    })


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
            with transaction.atomic():
                for line_form in line_formset:
                    if not line_form.cleaned_data or line_form.cleaned_data.get("DELETE"):
                        continue
                    data = line_form.cleaned_data
                    receipt = FabricReceipt(
                        received_date=header_form.cleaned_data["received_date"],
                        supplier=header_form.cleaned_data["supplier"],
                        fabric_name=data["fabric_name"],
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
                    create_fabric_rolls(receipt)
                    total_rolls += receipt.roll_count
                    saved_lines += 1
            messages.success(
                request,
                f"{total_rolls} fabric rolls across {saved_lines} fabric types received successfully.",
            )
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
@permission_required("production.view_productionproject", raise_exception=True)
def project_list(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    projects = ProductionProject.objects.select_related("finished_item", "color", "created_by")
    if q:
        projects = projects.filter(
            Q(project_no__icontains=q)
            | Q(finished_item__name__icontains=q)
            | Q(color__name__icontains=q)
        )
    if status:
        projects = projects.filter(status=status)
    return render(request, "production/project_list.html", {
        "projects": projects,
        "q": q,
        "status": status,
        "status_choices": ProductionProject.STATUS_CHOICES,
        "can_view_cost": request.user.has_perm("production.view_production_cost"),
    })


@login_required
@permission_required("production.add_productionproject", raise_exception=True)
def project_create(request):
    if request.method == "POST":
        form = ProductionProjectForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.created_by = request.user
            project.status = ProductionProject.STATUS_CUTTING
            project.save()
            messages.success(request, "Production project created.")
            return redirect("production_project_detail", pk=project.pk)
    else:
        form = ProductionProjectForm()
    return render(request, "production/project_form.html", {"form": form})


@login_required
@permission_required("production.view_productionproject", raise_exception=True)
def project_detail(request, pk):
    project = get_object_or_404(
        ProductionProject.objects.select_related("finished_item", "color", "created_by"),
        pk=pk,
    )
    sizes = list(_active_sizes())
    cut_map = {line.size_id: line.cut_qty for line in project.cut_sizes.all()}
    size_rows = [{"size": size, "cut_qty": cut_map.get(size.id, 0)} for size in sizes]
    roll_form = CuttingRollUsageForm(project=project)
    staff_form = StaffPayableForm()
    jobs = project.sewing_jobs.select_related("partner").prefetch_related("lines", "returns__lines")
    job_rows = []
    for job in jobs:
        job_rows.append({"job": job, "sizes": job_size_summary(job)})
    return render(request, "production/project_detail.html", {
        "project": project,
        "roll_form": roll_form,
        "size_rows": size_rows,
        "job_rows": job_rows,
        "staff_form": staff_form,
        "staff_payables": project.payables.filter(payable_type=ProductionPayable.TYPE_STAFF),
        "can_view_cost": request.user.has_perm("production.view_production_cost"),
        "can_manage_payments": request.user.has_perm("production.manage_production_payments"),
    })


@login_required
@permission_required("production.add_cuttingrollusage", raise_exception=True)
def project_add_roll(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)
    if project.status not in [ProductionProject.STATUS_DRAFT, ProductionProject.STATUS_CUTTING, ProductionProject.STATUS_CUT_COMPLETE]:
        messages.error(request, "Fabric rolls cannot be changed after sending to sewing.")
        return redirect("production_project_detail", pk=pk)
    form = CuttingRollUsageForm(request.POST or None, project=project)
    if request.method == "POST" and form.is_valid():
        usage = form.save(commit=False)
        usage.project = project
        usage.save()
        project.status = ProductionProject.STATUS_CUTTING
        project.save(update_fields=["status", "updated_at"])
        messages.success(request, "Fabric roll added to the cutting project.")
    else:
        for error in form.errors.values():
            messages.error(request, " ".join(error))
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
@permission_required("production.change_cuttingsizeline", raise_exception=True)
def project_save_cut_sizes(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)
    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)
    with transaction.atomic():
        for size in _active_sizes():
            raw = request.POST.get(f"cut_{size.id}", "0")
            try:
                qty = max(int(raw or 0), 0)
            except ValueError:
                qty = 0
            CuttingSizeLine.objects.update_or_create(
                project=project,
                size=size,
                defaults={"cut_qty": qty},
            )
    messages.success(request, "Cut quantities saved.")
    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required("production.change_productionproject", raise_exception=True)
def project_confirm_cutting(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)
    if request.method == "POST":
        try:
            confirm_cutting(project, request.user)
            messages.success(request, "Cutting confirmed. Remaining partial rolls returned to material stock.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    return redirect("production_project_detail", pk=pk)


@login_required
@permission_required("production.view_sewingpartner", raise_exception=True)
def partner_list(request):
    return render(request, "production/partner_list.html", {"partners": SewingPartner.objects.all()})


@login_required
@permission_required("production.add_sewingpartner", raise_exception=True)
def partner_create(request):
    if request.method == "POST":
        form = SewingPartnerForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Sewing partner created.")
            return redirect("production_partner_list")
    else:
        form = SewingPartnerForm()
    return render(request, "production/partner_form.html", {"form": form})


@login_required
@permission_required("production.add_sewingjob", raise_exception=True)
def sewing_job_create(request, project_id):
    project = get_object_or_404(ProductionProject, pk=project_id)
    if project.status not in [ProductionProject.STATUS_CUT_COMPLETE, ProductionProject.STATUS_SENT, ProductionProject.STATUS_PARTIAL_RETURN]:
        messages.error(request, "Confirm cutting before sending pieces to sewing.")
        return redirect("production_project_detail", pk=project_id)
    sizes = list(_active_sizes())
    cut_map = {line.size_id: int(line.cut_qty or 0) for line in project.cut_sizes.all()}
    already_sent = {
        row["size_id"]: row["total"]
        for row in SewingJobLine.objects.filter(job__project=project).values("size_id").annotate(total=Sum("sent_qty"))
    }
    rows = [{"size": size, "available": max(cut_map.get(size.id, 0) - int(already_sent.get(size.id, 0)), 0)} for size in sizes]

    if request.method == "POST":
        form = SewingJobForm(request.POST, user=request.user)
        entered = {}
        for row in rows:
            try:
                entered[row["size"].id] = max(int(request.POST.get(f"sent_{row['size'].id}") or 0), 0)
            except ValueError:
                entered[row["size"].id] = 0
        if form.is_valid():
            errors = []
            total = 0
            for row in rows:
                qty = entered[row["size"].id]
                total += qty
                if qty > row["available"]:
                    errors.append(f"{row['size'].name}: only {row['available']} cut pieces remain available.")
            if total <= 0:
                errors.append("Enter at least one piece to send.")
            if errors:
                for error in errors:
                    messages.error(request, error)
            else:
                with transaction.atomic():
                    job = form.save(commit=False)
                    job.project = project
                    job.created_by = request.user
                    if "price_per_piece" not in form.cleaned_data:
                        job.price_per_piece = Decimal("0")
                    job.save()
                    for size_id, qty in entered.items():
                        if qty > 0:
                            SewingJobLine.objects.create(job=job, size_id=size_id, sent_qty=qty)
                    project.status = ProductionProject.STATUS_SENT
                    project.save(update_fields=["status", "updated_at"])
                messages.success(request, "Cut pieces sent to sewing partner.")
                return redirect("production_project_detail", pk=project.pk)
    else:
        form = SewingJobForm(user=request.user)
        entered = {}
    return render(request, "production/sewing_job_form.html", {
        "form": form,
        "project": project,
        "rows": rows,
        "entered": entered,
        "can_view_cost": request.user.has_perm("production.view_production_cost"),
    })


@login_required
@permission_required("production.change_sewingjob", raise_exception=True)
def sewing_job_edit(request, pk):
    job = get_object_or_404(SewingJob.objects.select_related("project", "partner"), pk=pk)
    if request.method == "POST":
        form = SewingJobForm(request.POST, instance=job, user=request.user)
        if form.is_valid():
            old_price = job.price_per_piece
            job = form.save(commit=False)
            if "price_per_piece" not in form.cleaned_data:
                job.price_per_piece = old_price
            job.save()
            sync_sewing_payables(job)
            messages.success(request, "Sewing job updated.")
            return redirect("production_project_detail", pk=job.project_id)
    else:
        form = SewingJobForm(instance=job, user=request.user)
    return render(request, "production/sewing_job_form.html", {
        "form": form,
        "project": job.project,
        "job": job,
        "rows": [],
        "can_view_cost": request.user.has_perm("production.view_production_cost"),
    })


@login_required
@permission_required("production.add_sewingreturn", raise_exception=True)
def sewing_return_create(request, job_id):
    job = get_object_or_404(SewingJob.objects.select_related("project", "partner"), pk=job_id)
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
    returns = SewingReturn.objects.select_related("job__project", "job__partner", "created_by", "stocked_by")
    return render(request, "production/sewing_return_list.html", {"returns": returns})


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
            messages.success(request, "Return confirmed and good pieces stocked into Cloth Inventory.")
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
