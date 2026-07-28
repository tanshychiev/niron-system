from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.http import JsonResponse
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
    ProductionSupplierForm,
    ProductionExpenseForm,
)
from .models import (
    CuttingRollUsage,
    CuttingSizeLine,
    FabricReceipt,
    FabricRoll,
    ProductionPayable,
    ProductionPaymentBatch,
    ProductionPlanSize,
    ProductionProject,
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
                        supplier_ref=header_form.cleaned_data["supplier"],
                        supplier=header_form.cleaned_data["supplier"].name,
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
    sizes = list(_active_sizes())
    entered_plan = {}
    if request.method == "POST":
        form = ProductionProjectForm(request.POST)
        for size in sizes:
            try:
                entered_plan[size.id] = max(int(request.POST.get(f"plan_{size.id}") or 0), 0)
            except ValueError:
                entered_plan[size.id] = 0
        plan_total = sum(entered_plan.values())
        if form.is_valid() and plan_total > 0:
            with transaction.atomic():
                project = form.save(commit=False)
                project.created_by = request.user
                project.status = ProductionProject.STATUS_CUTTING
                project.expected_qty = plan_total  # compatibility with existing reports
                project.save()
                for size_id, qty in entered_plan.items():
                    if qty > 0:
                        ProductionPlanSize.objects.create(
                            project=project, size_id=size_id, planned_qty=qty
                        )
            messages.success(request, "Production project and size plan created.")
            return redirect("production_project_detail", pk=project.pk)
        if plan_total <= 0:
            messages.error(request, "Enter at least one planned quantity by size.")
    else:
        form = ProductionProjectForm()
    return render(request, "production/project_form.html", {
        "form": form,
        "plan_rows": [{"size": size, "value": entered_plan.get(size.id, 0)} for size in sizes],
    })


@login_required
@permission_required("production.view_productionproject", raise_exception=True)
def project_detail(request, pk):
    project = get_object_or_404(
        ProductionProject.objects.select_related("finished_item", "color", "created_by"),
        pk=pk,
    )
    active_sizes = {size.id: size for size in _active_sizes()}
    plan_map = {
        line.size_id: int(line.planned_qty or 0)
        for line in project.plan_sizes.select_related("size").filter(planned_qty__gt=0)
    }
    cut_map = {
        line.size_id: int(line.cut_qty or 0)
        for line in project.cut_sizes.select_related("size").filter(cut_qty__gt=0)
    }
    done_map = {
        row["size_id"]: int(row["total"] or 0)
        for row in SewingReturnLine.objects.filter(
            sewing_return__job__project=project,
            sewing_return__status=SewingReturn.STATUS_STOCKED,
        ).values("size_id").annotate(total=Sum("good_qty"))
    }

    # Show only sizes that belong to this project.
    used_size_ids = set(plan_map) | set(cut_map) | set(done_map)
    size_rows = []
    for size_id in sorted(
        used_size_ids,
        key=lambda pk: (
            getattr(active_sizes.get(pk), "sort_order", 999999),
            pk,
        ),
    ):
        size = active_sizes.get(size_id)
        if not size:
            continue
        planned = plan_map.get(size_id, 0)
        cut = cut_map.get(size_id, 0)
        done = done_map.get(size_id, 0)
        size_rows.append({
            "size": size,
            "planned_qty": planned,
            "cut_qty": cut,
            "done_qty": done,
            "pending_qty": max(planned - done, 0),
        })

    # Group selectable physical rolls by fabric + colour + remaining quantity.
    # The roll records and codes remain separate in the database.
    available_rolls = (
        FabricRoll.objects.select_related("receipt", "receipt__color")
        .filter(
            receipt__color=project.color,
            remaining_qty__gt=0,
        )
        .exclude(cutting_usages__applied=False)
        .order_by("receipt__fabric_name", "-remaining_qty", "created_at", "id")
    )

    grouped_map = OrderedDict()
    for roll in available_rolls:
        remaining = Decimal(roll.remaining_qty or 0)
        key = (roll.receipt.fabric_name.strip().lower(), remaining)
        if key not in grouped_map:
            grouped_map[key] = {
                "fabric_name": roll.receipt.fabric_name,
                "color": roll.receipt.color,
                "remaining_qty": remaining,
                "is_full": remaining == Decimal(roll.original_qty or 0),
                "available_count": 0,
                "roll_codes": [],
            }
        grouped_map[key]["available_count"] += 1
        grouped_map[key]["roll_codes"].append(roll.roll_code)

    fabric_groups = list(grouped_map.values())

    # Compact grouped summary for rolls already selected by this project.
    selected_usages = list(
        project.roll_usages.select_related("roll__receipt", "roll__receipt__color")
        .order_by("roll__receipt__fabric_name", "-issued_qty", "roll__roll_code")
    )
    selected_map = OrderedDict()
    for usage in selected_usages:
        roll = usage.roll
        key = (
            roll.receipt.fabric_name.strip().lower(),
            Decimal(usage.issued_qty or 0),
            bool(usage.applied),
        )
        if key not in selected_map:
            selected_map[key] = {
                "fabric_name": roll.receipt.fabric_name,
                "color": roll.receipt.color,
                "reserved_qty": Decimal(usage.issued_qty or 0),
                "count": 0,
                "applied": usage.applied,
                "usages": [],
            }
        selected_map[key]["count"] += 1
        selected_map[key]["usages"].append(usage)

    selected_groups = list(selected_map.values())

    jobs = project.sewing_jobs.select_related("partner").prefetch_related("lines", "returns__lines")
    job_rows = [{"job": job, "sizes": job_size_summary(job)} for job in jobs]

    return render(request, "production/project_detail.html", {
        "project": project,
        "size_rows": size_rows,
        "fabric_groups": fabric_groups,
        "selected_groups": selected_groups,
        "selected_usages": selected_usages,
        "job_rows": job_rows,
    })


@login_required
@permission_required("production.add_cuttingrollusage", raise_exception=True)
def project_add_roll(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)
    if project.status not in [
        ProductionProject.STATUS_DRAFT,
        ProductionProject.STATUS_CUTTING,
        ProductionProject.STATUS_CUT_COMPLETE,
    ]:
        messages.error(request, "Fabric rolls cannot be changed after sending to sewing.")
        return redirect("production_project_detail", pk=pk)

    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    fabric_name = (request.POST.get("fabric_name") or "").strip()
    remaining_raw = (request.POST.get("remaining_qty") or "").strip()
    count_raw = (request.POST.get("reserve_count") or "").strip()
    note = (request.POST.get("note") or "").strip()

    try:
        remaining_qty = Decimal(remaining_raw)
        reserve_count = int(count_raw)
    except (InvalidOperation, TypeError, ValueError):
        messages.error(request, "Choose a valid fabric group and quantity.")
        return redirect("production_project_detail", pk=pk)

    if not fabric_name or remaining_qty <= 0 or reserve_count <= 0:
        messages.error(request, "Choose at least one available fabric roll.")
        return redirect("production_project_detail", pk=pk)

    try:
        with transaction.atomic():
            candidates = list(
                FabricRoll.objects.select_for_update()
                .select_related("receipt", "receipt__color")
                .filter(
                    receipt__color=project.color,
                    receipt__fabric_name__iexact=fabric_name,
                    remaining_qty=remaining_qty,
                    remaining_qty__gt=0,
                )
                .exclude(cutting_usages__applied=False)
                .order_by("created_at", "id")[:reserve_count]
            )

            if len(candidates) < reserve_count:
                raise ValidationError(
                    f"Only {len(candidates)} roll(s) remain available for "
                    f"{fabric_name} at {remaining_qty.normalize()} each."
                )

            for roll in candidates:
                CuttingRollUsage.objects.create(
                    project=project,
                    roll=roll,
                    issued_qty=Decimal(roll.remaining_qty or 0),
                    returned_qty=Decimal("0"),
                    note=note,
                )

        messages.success(
            request,
            f"{reserve_count} roll(s) reserved from {fabric_name}. "
            "Individual roll codes were selected automatically.",
        )
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
    project = get_object_or_404(ProductionProject, pk=pk)
    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)
    values = {}
    for size in _active_sizes():
        field_name = f"plan_{size.id}"
        if field_name not in request.POST:
            continue
        try:
            qty = max(int(request.POST.get(field_name) or 0), 0)
        except ValueError:
            qty = 0
        if qty > 0:
            values[size.id] = qty

    total = sum(values.values())
    if total <= 0:
        messages.error(request, "Production plan must contain at least one size and quantity.")
        return redirect("production_project_detail", pk=pk)

    with transaction.atomic():
        # Remove rows deleted from the dynamic size list, but never remove a size
        # that already has cutting activity.
        protected_size_ids = set(
            project.cut_sizes.filter(cut_qty__gt=0).values_list("size_id", flat=True)
        )
        removable = project.plan_sizes.exclude(size_id__in=values.keys()).exclude(
            size_id__in=protected_size_ids
        )
        removable.delete()

        for size_id, qty in values.items():
            ProductionPlanSize.objects.update_or_create(
                project=project,
                size_id=size_id,
                defaults={"planned_qty": qty},
            )

        project.expected_qty = total
        project.save(update_fields=["expected_qty", "updated_at"])
    messages.success(request, "Production plan updated.")
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
    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)

    try:
        with transaction.atomic():
            # Save actual cut quantities in the same action so the user does not
            # need to press a separate Save button before Confirm Cutting.
            cut_total = 0
            plan_lines = list(project.plan_sizes.select_related("size").all())
            for plan_line in plan_lines:
                raw = request.POST.get(f"cut_{plan_line.size_id}", "0")
                try:
                    qty = max(int(raw or 0), 0)
                except (TypeError, ValueError):
                    raise ValidationError(f"Enter a valid cut quantity for {plan_line.size.name}.")

                if qty > int(plan_line.planned_qty or 0):
                    raise ValidationError(
                        f"{plan_line.size.name}: cut quantity cannot exceed planned quantity {plan_line.planned_qty}."
                    )

                CuttingSizeLine.objects.update_or_create(
                    project=project,
                    size=plan_line.size,
                    defaults={"cut_qty": qty},
                )
                cut_total += qty

            if cut_total <= 0:
                raise ValidationError("Enter the actual cut quantity by size first.")

            # For every reserved roll, record whether fabric remains after cutting.
            usages = list(project.roll_usages.select_related("roll").filter(applied=False))
            if not usages:
                raise ValidationError("Reserve at least one fabric roll first.")

            for usage in usages:
                remain_choice = request.POST.get(f"remain_choice_{usage.id}", "")
                if remain_choice not in {"NO", "YES"}:
                    raise ValidationError(
                        f"Choose whether fabric remains for roll {usage.roll.roll_code}."
                    )

                if remain_choice == "NO":
                    returned_qty = Decimal("0")
                else:
                    raw_remaining = request.POST.get(f"remaining_qty_{usage.id}", "")
                    try:
                        returned_qty = Decimal(str(raw_remaining))
                    except (InvalidOperation, TypeError, ValueError):
                        raise ValidationError(
                            f"Enter a valid remaining quantity for roll {usage.roll.roll_code}."
                        )
                    if returned_qty <= 0:
                        raise ValidationError(
                            f"Remaining quantity for roll {usage.roll.roll_code} must be greater than zero."
                        )
                    if returned_qty >= Decimal(usage.issued_qty or 0):
                        raise ValidationError(
                            f"Remaining quantity for roll {usage.roll.roll_code} must be less than the reserved quantity."
                        )

                usage.returned_qty = returned_qty
                usage.save(update_fields=["returned_qty"])

            confirm_cutting(project, request.user)

        messages.success(
            request,
            "Cutting confirmed. Used fabric was consumed and remaining fabric was returned to material stock.",
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))

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
