from django.urls import path

from .views import (
    color_create,
    color_edit,
    color_list,
    inventory_adjust_stock_select,
    inventory_adjustment_create,
    inventory_adjustment_list,
    inventory_batch_create,
    inventory_batch_delete,
    inventory_batch_detail,
    inventory_batch_edit,
    inventory_batch_history,
    inventory_item_create,
    inventory_item_edit,
    inventory_item_list,
    inventory_item_delete,   # ✅ add this
    inventory_list,
    material_usage,
    size_create,
    size_edit,
    size_list,
)

urlpatterns = [
    # Dashboard
    path("", inventory_list, name="inventory_list"),

    # Items
    path("items/", inventory_item_list, name="inventory_item_list"),
    path("items/new/", inventory_item_create, name="inventory_item_create"),
    path("items/<int:pk>/edit/", inventory_item_edit, name="inventory_item_edit"),
    path("items/<int:pk>/delete/", inventory_item_delete, name="inventory_item_delete"),  # ✅ FIXED

    # Colors
    path("colors/", color_list, name="color_list"),
    path("colors/new/", color_create, name="color_create"),
    path("colors/<int:pk>/edit/", color_edit, name="color_edit"),

    # Sizes
    path("sizes/", size_list, name="size_list"),
    path("sizes/new/", size_create, name="size_create"),
    path("sizes/<int:pk>/edit/", size_edit, name="size_edit"),

    # Stock In (Batch)
    path("batches/new/", inventory_batch_create, name="inventory_batch_create"),
    path("batches/<int:pk>/", inventory_batch_detail, name="inventory_batch_detail"),
    path("batches/<int:pk>/edit/", inventory_batch_edit, name="inventory_batch_edit"),
    path("batches/<int:pk>/delete/", inventory_batch_delete, name="inventory_batch_delete"),
    path("batches/<int:pk>/history/", inventory_batch_history, name="inventory_batch_history"),

    # Adjust
    path("adjust-stock/", inventory_adjust_stock_select, name="inventory_adjust_stock_select"),
    path("adjustments/", inventory_adjustment_list, name="inventory_adjustment_list"),
    path("adjustments/new/<int:batch_item_id>/", inventory_adjustment_create, name="inventory_adjustment_create"),

    # Material usage
    path("material-usage/", material_usage, name="material_usage"),
]