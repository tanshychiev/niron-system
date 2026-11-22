"""Read-only history of production fabric and Borib by project.

No stock movements are created here. Data originates from the established
CuttingRollUsage, CuttingBoribUsage and FabricReceipt transactions.
"""
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Q
from django.shortcuts import render

from production.models import (
    BoribStock, CuttingBoribUsage, CuttingRollUsage, FabricReceipt,
    ProductionProject,
)

ZERO = Decimal('0')


def _price(value):
    try:
        result = Decimal(str(value or '0'))
        return result if ZERO <= result <= Decimal('1000000') else ZERO
    except (ValueError, InvalidOperation):
        return ZERO


@login_required
@permission_required('production.view_productionproject', raise_exception=True)
def material_history(request):
    """Project-specific fabric/Borib material quantities and cost overview.

    Showing costs requires the existing production cost or finance permission.
    The optional Borib rate is a what-if estimate, NOT an actual purchase cost.
    """
    user = request.user
    can_view_cost = (
        user.is_superuser
        or user.has_perm('production.view_production_cost')
        or user.has_perm('finance.view_expense')
    )
    q = (request.GET.get('q') or '').strip()[:100]
    project_id = (request.GET.get('project') or '').strip()
    kind = request.GET.get('kind') or 'all'
    if kind not in ('all', 'fabric', 'borib'):
        kind = 'all'
    borib_rate = _price(request.GET.get('borib_rate')) if can_view_cost else ZERO

    projects = ProductionProject.objects.select_related('finished_item').order_by('-created_at', '-pk')
    if q:
        projects = projects.filter(Q(project_no__icontains=q) | Q(finished_item__name__icontains=q))
    if project_id:
        if project_id.isdigit():
            projects = projects.filter(Q(pk=int(project_id)) | Q(project_no__icontains=project_id))
        else:
            projects = projects.filter(project_no__icontains=project_id)
    # Bounded report: for a complete historical export, add pagination.
    projects = list(projects[:100])
    ids = [p.pk for p in projects]

    fabric = []
    borib = []
    if kind in ('all', 'fabric'):
        fabric = list(CuttingRollUsage.objects.filter(project_id__in=ids)
            .select_related('project', 'project_color__color', 'roll__receipt__color')
            .order_by('-created_at', '-pk'))
    if kind in ('all', 'borib'):
        borib = list(CuttingBoribUsage.objects.filter(project_id__in=ids)
            .select_related('project', 'project_color__color')
            .order_by('-created_at', '-pk'))

    report = {p.pk: {
        'project': p, 'fabric_kg': ZERO, 'fabric_returned_kg': ZERO,
        'fabric_used_kg': ZERO, 'fabric_cost': ZERO, 'borib_kg': ZERO,
        'borib_estimate': ZERO, 'fabric_lines': 0, 'borib_lines': 0,
    } for p in projects}
    rows = []
    for movement in fabric:
        entry = report[movement.project_id]
        issued = Decimal(movement.issued_qty or 0)
        returned = Decimal(movement.returned_qty or 0)
        consumed = (issued - returned) if movement.applied else ZERO
        # FabricReceipt.cost_per_kg includes configured goods, shipping and extra
        # receipt charges; it is a recalculated receipt allocation, not a frozen
        # historical unit-cost snapshot.
        unit_price = Decimal(movement.roll.receipt.cost_per_kg) if can_view_cost else ZERO
        cost = consumed * unit_price
        entry['fabric_kg'] += issued if movement.applied else ZERO
        entry['fabric_returned_kg'] += returned if movement.applied else ZERO
        entry['fabric_used_kg'] += consumed
        entry['fabric_cost'] += cost
        entry['fabric_lines'] += 1
        rows.append({
            'date': movement.created_at,
            'project': movement.project,
            'kind': 'Fabric',
            'reference': movement.roll.roll_code,
            'item': movement.roll.receipt.fabric_name,
            'color': movement.roll.receipt.color.name,
            'unit': 'kg', 'issued': issued, 'returned': returned,
            'used': consumed, 'cost': cost, 'unit_price': unit_price,
            'is_estimate': False, 'applied': movement.applied,
        })
    for movement in borib:
        entry = report[movement.project_id]
        used = Decimal(movement.quantity_kg or 0)
        estimate = used * borib_rate
        entry['borib_kg'] += used
        entry['borib_estimate'] += estimate
        entry['borib_lines'] += 1
        rows.append({
            'date': movement.updated_at,
            'project': movement.project,
            'kind': 'Borib', 'reference': 'CUT-BORIB-%s' % movement.pk,
            'item': 'Borib', 'color': movement.project_color.color.name,
            'unit': 'kg', 'issued': used, 'returned': ZERO, 'used': used,
            'cost': estimate, 'unit_price': borib_rate,
            'is_estimate': True, 'applied': True,
        })
    rows.sort(key=lambda item: (item['date'], item['reference']), reverse=True)

    for item in report.values():
        item['total_estimated'] = item['fabric_cost'] + item['borib_estimate']

    totals = {
        'fabric_used_kg': sum((r['fabric_used_kg'] for r in report.values()), ZERO),
        'fabric_cost': sum((r['fabric_cost'] for r in report.values()), ZERO),
        'borib_kg': sum((r['borib_kg'] for r in report.values()), ZERO),
        'borib_estimate': sum((r['borib_estimate'] for r in report.values()), ZERO),
    }
    totals['estimated_material_cost'] = totals['fabric_cost'] + totals['borib_estimate']
    return render(request, 'inventory/material_history.html', {
        'projects': list(report.values()), 'rows': rows, 'totals': totals,
        'q': q, 'project_id': project_id, 'kind': kind, 'borib_rate': borib_rate,
        'can_view_cost': can_view_cost, 'project_count': len(projects),
        'report_limited': len(projects) == 100,
    })
