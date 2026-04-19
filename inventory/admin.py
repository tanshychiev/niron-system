from django.contrib import admin
from .models import Color, InventoryBatch, InventoryBatchItem, InventoryItem, Size


class InventoryBatchItemInline(admin.TabularInline):
    model = InventoryBatchItem
    extra = 1


@admin.register(Size)
class SizeAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "sort_order", "is_active"]


@admin.register(Color)
class ColorAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "is_active"]


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "item_type", "unit", "is_active", "total_stock"]


@admin.register(InventoryBatch)
class InventoryBatchAdmin(admin.ModelAdmin):
    list_display = ["batch_no", "supplier", "received_date", "status", "total_expense"]
    inlines = [InventoryBatchItemInline]