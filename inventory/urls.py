from django.urls import path

from .stock_confirm import stock_confirm, stock_history, stock_history_detail
from .stock_summary import stock_summary_report
from .views import (
    color_create, color_edit, color_list,
    correct_stock_count_view,
    inventory_adjust_stock_select, inventory_adjustment_create, inventory_adjustment_list,
    inventory_batch_add_cost, inventory_batch_confirm_received, inventory_batch_create,
    inventory_batch_delete, inventory_batch_detail, inventory_batch_edit, inventory_batch_history,
    inventory_stock_in_list, inventory_supplier_ajax_create,
    inventory_item_create, inventory_item_delete, inventory_item_edit, inventory_item_list,
    inventory_list, material_usage,
    size_create, size_edit, size_list,
    stock_ledger_by_batch_item, stock_ledger_list,
)

urlpatterns = [
    path("", inventory_list, name="inventory_list"),
    path("stock-confirm/", stock_confirm, name="stock_confirm"),
    path("stock-summary/", stock_summary_report, name="stock_summary_report"),
    path("stock-history/", stock_history, name="stock_history"),
    path("stock-history/<int:year>/<int:month>/<int:day>/", stock_history_detail, name="stock_history_detail"),
    path("items/", inventory_item_list, name="inventory_item_list"),
    path("items/new/", inventory_item_create, name="inventory_item_create"),
    path("items/<int:pk>/edit/", inventory_item_edit, name="inventory_item_edit"),
    path("items/<int:pk>/delete/", inventory_item_delete, name="inventory_item_delete"),
    path("colors/", color_list, name="color_list"),
    path("colors/new/", color_create, name="color_create"),
    path("colors/<int:pk>/edit/", color_edit, name="color_edit"),
    path("sizes/", size_list, name="size_list"),
    path("sizes/new/", size_create, name="size_create"),
    path("sizes/<int:pk>/edit/", size_edit, name="size_edit"),

    # Stock In / Purchase
    path("batches/new/", inventory_batch_create, name="inventory_batch_create"),
    path("stock-in-list/", inventory_stock_in_list, name="inventory_stock_in_list"),
    path("batches/<int:pk>/confirm-received/", inventory_batch_confirm_received, name="inventory_batch_confirm_received"),
    path("batches/<int:pk>/add-cost/", inventory_batch_add_cost, name="inventory_batch_add_cost"),
    path("suppliers/ajax-create/", inventory_supplier_ajax_create, name="inventory_supplier_ajax_create"),
    path("batches/<int:pk>/", inventory_batch_detail, name="inventory_batch_detail"),
    path("batches/<int:pk>/edit/", inventory_batch_edit, name="inventory_batch_edit"),
    path("batches/<int:pk>/delete/", inventory_batch_delete, name="inventory_batch_delete"),
    path("batches/<int:pk>/history/", inventory_batch_history, name="inventory_batch_history"),

    path("adjust-stock/", inventory_adjust_stock_select, name="inventory_adjust_stock_select"),
    path("adjustments/", inventory_adjustment_list, name="inventory_adjustment_list"),
    path("adjustments/new/<int:batch_item_id>/", inventory_adjustment_create, name="inventory_adjustment_create"),
    path("material-usage/", material_usage, name="material_usage"),
    path("ledger/", stock_ledger_list, name="stock_ledger_list"),
    path("ledger/batch-item/<int:batch_item_id>/", stock_ledger_by_batch_item, name="stock_ledger_by_batch_item"),
    path("ledger/batch-item/<int:batch_item_id>/correct/", correct_stock_count_view, name="correct_stock_count"),
]
