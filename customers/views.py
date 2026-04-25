from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Count, Sum, Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render

from .models import Customer
from orders.models import Order
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render


@login_required
@permission_required("customers.view_customer", raise_exception=True)
def customer_list(request):
    q = (request.GET.get("q") or "").strip()

    customers = Customer.objects.annotate(
        total_orders=Count("orders"),
        total_paid=Sum("orders__paid_amount"),
        last_order_at=Max("orders__created_at"),
    )

    if q:
        customers = customers.filter(
            Q(name__icontains=q)
            | Q(phone__icontains=q)
            | Q(location__icontains=q)
        )

    return render(request, "customers/customer_list.html", {
        "customers": customers,
        "q": q,
    })


@login_required
@permission_required("customers.view_customer", raise_exception=True)
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    orders = Order.objects.filter(customer=customer).order_by("-created_at", "-id")

    total_paid = orders.aggregate(total=Sum("paid_amount")).get("total") or 0

    return render(request, "customers/customer_detail.html", {
        "customer": customer,
        "orders": orders,
        "total_orders": orders.count(),
        "total_paid": total_paid,
    })


@login_required
def customer_search(request):
    q = (request.GET.get("q") or "").strip()

    customers = Customer.objects.filter(name__icontains=q).order_by("name")[:10]

    data = [
        {
            "id": c.id,
            "name": c.name,
            "phone": c.phone,
            "location": c.location,
        }
        for c in customers
    ]

    return JsonResponse(data, safe=False)

@login_required
@permission_required("customers.change_customer", raise_exception=True)
def customer_edit(request, pk):
    customer = get_object_or_404(Customer, pk=pk)

    if request.method == "POST":
        customer.name = (request.POST.get("name") or "").strip()
        customer.phone = (request.POST.get("phone") or "").strip()
        customer.location = (request.POST.get("location") or "").strip()

        if not customer.name:
            messages.error(request, "Customer name is required.")
            return render(request, "customers/customer_form.html", {"customer": customer})

        customer.save(update_fields=["name", "phone", "location", "updated_at"])
        messages.success(request, "Customer updated successfully.")
        return redirect("customer_list")

    return render(request, "customers/customer_form.html", {"customer": customer})