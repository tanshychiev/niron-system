from django.db.models import Sum
from orders.models import Order
from .models import Expense
from inventory.models import InventoryItem


def build_dashboard():
    revenue = Order.objects.aggregate(total=Sum("price"))["total"] or 0
    total_expense = Expense.objects.aggregate(total=Sum("amount"))["total"] or 0

    profit = revenue - total_expense

    total_cloth_sold = Order.objects.aggregate(total=Sum("quantity"))["total"] or 0
    total_inventory = InventoryItem.objects.aggregate(total=Sum("quantity"))["total"] or 0

    return {
        "revenue": revenue,
        "expense": total_expense,
        "profit": profit,
        "cloth_sold": total_cloth_sold,
        "inventory": total_inventory,
    }