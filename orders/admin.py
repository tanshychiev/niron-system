from django.contrib import admin

from .models import (
    Order,
    OrderDesign,
    OrderItem,
    OrderDesignFile,
    OrderProgress,
    OrderHistory,
    OrderPaymentLog,
    StockConsumption,
)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    fields = (
        "design",
        "description",
        "shirt_item",
        "color",
        "size",
        "film_item",
        "film_meter",
        "material_item",
        "quantity",
        "done_qty",
        "unit_price",
        "line_total",
    )
    readonly_fields = ("line_total",)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_no",
        "invoice_date",
        "customer_name",
        "phone",
        "deadline",
        "service_type",
        "payment_status",
        "status",
        "total_amount",
        "created_at",
    )

    list_editable = (
        "invoice_date",
        "status",
    )

    list_filter = (
        "invoice_date",
        "deadline",
        "service_type",
        "payment_status",
        "status",
        "created_at",
    )

    search_fields = (
        "order_no",
        "customer_name",
        "phone",
        "customer_location",
    )

    readonly_fields = (
        "order_no",
        "payment_status",
        "created_at",
    )

    fields = (
        "order_no",
        "invoice_date",
        "order_type",
        "service_type",
        "customer",
        "customer_name",
        "phone",
        "customer_location",
        "deadline",
        "total_amount",
        "deposit_amount",
        "paid_amount",
        "payment_status",
        "status",
        "remark",
        "created_by",
        "created_at",
        "stock_deducted",
        "total_pcs",
        "done_pcs",
        "is_deleted",
        "deleted_at",
        "deleted_reason",
        "deleted_by",
    )

    ordering = (
        "-invoice_date",
        "-id",
    )

    date_hierarchy = "invoice_date"

    inlines = [
        OrderItemInline,
    ]


@admin.register(OrderDesign)
class OrderDesignAdmin(admin.ModelAdmin):
    list_display = ("order", "name", "sort_order", "created_at")
    search_fields = ("order__order_no", "name")
    list_filter = ("created_at",)


@admin.register(OrderDesignFile)
class OrderDesignFileAdmin(admin.ModelAdmin):
    list_display = ("order", "design", "image", "uploaded_at")
    search_fields = ("order__order_no",)
    list_filter = ("uploaded_at",)


@admin.register(OrderProgress)
class OrderProgressAdmin(admin.ModelAdmin):
    list_display = ("order", "order_item", "qty_done", "remark", "created_at")
    search_fields = ("order__order_no", "remark")
    list_filter = ("created_at",)


@admin.register(OrderHistory)
class OrderHistoryAdmin(admin.ModelAdmin):
    list_display = ("order", "action", "field_name", "changed_by", "created_at")
    search_fields = ("order__order_no", "field_name", "old_value", "new_value", "remark")
    list_filter = ("action", "created_at")


@admin.register(OrderPaymentLog)
class OrderPaymentLogAdmin(admin.ModelAdmin):
    list_display = ("order", "action", "amount", "created_by", "created_at")
    search_fields = ("order__order_no", "reason")
    list_filter = ("action", "created_at")


@admin.register(StockConsumption)
class StockConsumptionAdmin(admin.ModelAdmin):
    list_display = ("order", "order_item", "batch_item", "consumed_qty", "unit_cost", "created_at")
    search_fields = ("order__order_no",)
    list_filter = ("created_at",)