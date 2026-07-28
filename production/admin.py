from django.contrib import admin

from .models import (
    CuttingRollUsage,
    CuttingSizeLine,
    FabricReceipt,
    FabricRoll,
    ProductionPayable,
    ProductionPaymentAllocation,
    ProductionPaymentBatch,
    ProductionProject,
    SewingJob,
    SewingJobLine,
    SewingPartner,
    SewingReturn,
    SewingReturnLine,
    ProductionSupplier,
    ProductionExpense,
)


class FabricRollInline(admin.TabularInline):
    model = FabricRoll
    extra = 0
    readonly_fields = ("roll_code", "status", "created_at")


@admin.register(FabricReceipt)
class FabricReceiptAdmin(admin.ModelAdmin):
    list_display = ("receipt_no", "received_date", "supplier", "fabric_name", "color", "roll_count", "total_cost")
    list_filter = ("received_date", "color")
    search_fields = ("receipt_no", "supplier", "fabric_name")
    inlines = [FabricRollInline]


@admin.register(FabricRoll)
class FabricRollAdmin(admin.ModelAdmin):
    list_display = ("roll_code", "fabric_name", "color", "remaining_qty", "status")
    list_filter = ("status", "receipt__color")
    search_fields = ("roll_code", "receipt__fabric_name", "receipt__supplier")


class CuttingRollInline(admin.TabularInline):
    model = CuttingRollUsage
    extra = 0


class CuttingSizeInline(admin.TabularInline):
    model = CuttingSizeLine
    extra = 0


@admin.register(ProductionProject)
class ProductionProjectAdmin(admin.ModelAdmin):
    list_display = ("project_no", "finished_item", "color", "expected_qty", "status", "created_at")
    list_filter = ("status", "color")
    search_fields = ("project_no", "finished_item__name", "color__name")
    inlines = [CuttingRollInline, CuttingSizeInline]


class SewingJobLineInline(admin.TabularInline):
    model = SewingJobLine
    extra = 0


@admin.register(SewingJob)
class SewingJobAdmin(admin.ModelAdmin):
    list_display = ("job_no", "project", "partner", "sent_date", "price_per_piece", "status")
    list_filter = ("status", "partner")
    inlines = [SewingJobLineInline]


class SewingReturnLineInline(admin.TabularInline):
    model = SewingReturnLine
    extra = 0


@admin.register(SewingReturn)
class SewingReturnAdmin(admin.ModelAdmin):
    list_display = ("return_no", "job", "return_date", "good_total", "status", "stock_batch")
    list_filter = ("status", "return_date")
    inlines = [SewingReturnLineInline]


@admin.register(SewingPartner)
class SewingPartnerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "location", "is_active")
    search_fields = ("name", "phone")


@admin.register(ProductionPayable)
class ProductionPayableAdmin(admin.ModelAdmin):
    list_display = ("payable_type", "payee_name", "project", "amount", "paid_amount", "payment_status")
    list_filter = ("payable_type",)
    search_fields = ("payee_name", "project__project_no", "description")


class PaymentAllocationInline(admin.TabularInline):
    model = ProductionPaymentAllocation
    extra = 0
    readonly_fields = ("payable", "amount")


@admin.register(ProductionPaymentBatch)
class ProductionPaymentBatchAdmin(admin.ModelAdmin):
    list_display = ("payment_no", "payment_date", "payable_type", "payee_name", "total_amount", "payment_method", "finance_expense_id")
    list_filter = ("payable_type", "payment_method", "payment_date")
    search_fields = ("payment_no", "payee_name", "reference")
    inlines = [PaymentAllocationInline]


@admin.register(ProductionSupplier)
class ProductionSupplierAdmin(admin.ModelAdmin):
    list_display=("name","phone","location","is_active")
    search_fields=("name","phone","location")

@admin.register(ProductionExpense)
class ProductionExpenseAdmin(admin.ModelAdmin):
    list_display=("expense_date","category","supplier","sewing_partner","project","amount","finance_expense_id")
    list_filter=("category","expense_date")
    search_fields=("supplier__name","sewing_partner__name","project__project_no","note")
