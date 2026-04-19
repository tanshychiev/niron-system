from django.contrib import admin

from .models import Expense


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "created_at",
        "expense_type",
        "display_title",
        "amount",
        "created_by",
    )
    list_filter = ("expense_type", "category", "created_at")
    search_fields = ("note", "batch_label", "batch_ref", "created_by__username")