from django.urls import path
from . import views

urlpatterns = [
    path("expenses/", views.expense_list, name="expense_list"),
    path("expenses/create-other/", views.create_other_expense, name="create_other_expense"),
    path("expenses/create-batch/", views.create_batch_expense, name="create_batch_expense"),
    path("expenses/create-operating/", views.create_operating_expense, name="create_operating_expense"),
    path("profit-dashboard/", views.profit_dashboard, name="profit_dashboard"),
]