from django.urls import path
from . import views

urlpatterns = [
    path("expenses/", views.expense_summary, name="expense_summary"),
    path("expenses/other/", views.other_expense_list, name="other_expense_list"),
    path("expenses/batch/", views.batch_expense_list, name="batch_expense_list"),
    path("expenses/operating/", views.operating_expense_list, name="operating_expense_list"),

    path("expenses/create-other/", views.create_other_expense, name="create_other_expense"),
    path("expenses/create-batch/", views.create_batch_expense, name="create_batch_expense"),
    path("expenses/create-operating/", views.create_operating_expense, name="create_operating_expense"),

    path("expenses/batch-preview/", views.batch_expense_preview, name="batch_expense_preview"),

    path("profit-dashboard/", views.profit_dashboard, name="profit_dashboard"),
]