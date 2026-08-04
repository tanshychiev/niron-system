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
        ProductionProject.objects.select_related("finished_item", "fabric_type", "created_by")
        .prefetch_related("project_colors__color", "project_colors__plan_sizes__size", "project_colors__cut_sizes__size"),
        pk=pk,
    )
    sizes = list(_active_sizes())
    color_rows = []
    for pc in project.project_colors.select_related("color").all():
        plan = {x.size_id:int(x.planned_qty or 0) for x in pc.plan_sizes.all()}
        cut = {x.size_id:int(x.cut_qty or 0) for x in pc.cut_sizes.all()}
        done = {x["size_id"]:int(x["total"] or 0) for x in SewingReturnLine.objects.filter(
            sewing_return__job__project_color=pc,
            sewing_return__status=SewingReturn.STATUS_STOCKED,
        ).values("size_id").annotate(total=Sum("good_qty"))}
        sent = {x["size_id"]:int(x["total"] or 0) for x in SewingJobLine.objects.filter(job__project_color=pc).values("size_id").annotate(total=Sum("sent_qty"))}
        rows=[]
        for size in sizes:
            if plan.get(size.id,0) or cut.get(size.id,0) or done.get(size.id,0) or sent.get(size.id,0):
                rows.append({"size":size,"planned":plan.get(size.id,0),"cut":cut.get(size.id,0),"sent":sent.get(size.id,0),"done":done.get(size.id,0)})
        rolls = pc.roll_usages.select_related("roll__receipt").all()
        available = FabricRoll.objects.select_related("receipt","receipt__color","receipt__fabric_type").filter(
            receipt__color=pc.color, remaining_qty__gt=0
        ).exclude(cutting_usages__applied=False)
        if project.fabric_type_id:
            available = available.filter(receipt__fabric_type=project.fabric_type)
        color_rows.append({"pc":pc,"rows":rows,"rolls":rolls,"available_rolls":available.order_by("roll_code")})
    jobs = project.sewing_jobs.select_related("project_color__color","partner").prefetch_related("lines","returns__lines")
    job_rows=[{"job":j,"sizes":job_size_summary(j)} for j in jobs]
    return render(request,"production/project_detail.html",{
        "project":project,"color_rows":color_rows,"sizes":sizes,"job_rows":job_rows,
        "can_view_cost":request.user.has_perm("production.view_production_cost"),
    })

@login_required
@permission_required("production.add_cuttingrollusage", raise_exception=True)
def project_add_roll(request, pk):
    project = get_object_or_404(ProductionProject, pk=pk)
    if request.method != "POST":
        return redirect("production_project_detail", pk=pk)
    pc = get_object_or_404(ProductionProjectColor, pk=request.POST.get("project_color_id"), project=project)
    roll = get_object_or_404(FabricRoll.objects.select_related("receipt"), pk=request.POST.get("roll_id"))
    if roll.receipt.color_id != pc.color_id:
        messages.error(request, "The fabric roll colour does not match.")
        return redirect("production_project_detail", pk=pk)
    if project.fabric_type_id and roll.receipt.fabric_type_id != project.fabric_type_id:
        messages.error(request, "The fabric roll type does not match this project.")
        return redirect("production_project_detail", pk=pk)
    try:
        CuttingRollUsage.objects.create(
            project=project, project_color=pc, roll=roll,
            issued_qty=roll.available_qty, returned_qty=Decimal("0"),
            note=(request.POST.get("note") or "").strip(),
        )
        messages.success(request, f"{roll.roll_code} reserved for {pc.color.name}.")
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
    project=get_object_or_404(ProductionProject,pk=pk)
    if request.method != "POST": return redirect("production_project_detail",pk=pk)
    with transaction.atomic():
        for pc in project.project_colors.all():
            for size in _active_sizes():
                try: qty=max(int(request.POST.get(f"cut_{pc.id}_{size.id}") or 0),0)
                except ValueError: qty=0
                obj,_=CuttingSizeLine.objects.get_or_create(project=project,project_color=pc,size=size)
                if qty: obj.cut_qty=qty; obj.save(update_fields=["cut_qty"])
                else: obj.delete()
    messages.success(request,"Cut quantities updated.")
    return redirect("production_project_detail",pk=pk)

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