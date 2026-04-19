from django.contrib import admin
from .models import Expense


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "expense_type",
        "amount",
        "created_by",
        "batch",
        "category",
    )
    list_filter = ("expense_type", "category", "created_at")
    search_fields = ("note", "created_by__username")