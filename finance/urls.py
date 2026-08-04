from django.urls import path

from . import views


urlpatterns = [
    # Finance summary
    path(
        "expenses/",
        views.expense_summary,
        name="expense_summary",
    ),

    # Other Expense
    path(
        "expenses/other/",
        views.other_expense_list,
        name="other_expense_list",
    ),
    path(
        "expenses/create-other/",
        views.create_other_expense,
        name="create_other_expense",
    ),

    # Stock In Expense
    path(
        "expenses/stock-in/",
        views.batch_expense_list,
        name="batch_expense_list",
    ),
    path(
        "expenses/create-stock-in/",
        views.create_batch_expense,
        name="create_batch_expense",
    ),
    path(
        "expenses/stock-in/<int:pk>/cost/",
        views.batch_expense_cost_edit,
        name="batch_expense_cost_edit",
    ),
    path(
        "expenses/batch-preview/",
        views.batch_expense_preview,
        name="batch_expense_preview",
    ),

    # Keep the old URL working
    path(
        "expenses/batch/",
        views.batch_expense_list,
        name="batch_expense_list_old",
    ),

    # Operating Expense
    path(
        "expenses/operating/",
        views.operating_expense_list,
        name="operating_expense_list",
    ),
    path(
        "expenses/create-operating/",
        views.create_operating_expense,
        name="create_operating_expense",
    ),

    # Export
    path(
        "expenses/export-excel/",
        views.expense_summary_export_excel,
        name="expense_summary_export_excel",
    ),

    # Revenue and profit
    path(
        "revenue/",
        views.revenue_dashboard,
        name="revenue_dashboard",
    ),
    path(
        "profit-dashboard/",
        views.profit_dashboard,
        name="profit_dashboard",
    ),
]