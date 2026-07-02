from decimal import Decimal

from django import forms

from inventory.models import Color, InventoryItem

from .models import (
    CuttingRollUsage,
    FabricReceipt,
    FabricRoll,
    ProductionPaymentBatch,
    ProductionPayable,
    ProductionProject,
    SewingJob,
    SewingPartner,
    SewingReturn,
)


class FabricReceiptForm(forms.ModelForm):
    class Meta:
        model = FabricReceipt
        fields = [
            "received_date",
            "supplier",
            "fabric_name",
            "color",
            "roll_count",
            "total_goods_cost",
            "shipping_cost",
            "extra_cost",
            "note",
        ]
        widgets = {
            "received_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "supplier": forms.TextInput(attrs={"class": "form-control", "placeholder": "Supplier"}),
            "fabric_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Cotton 220 GSM"}),
            "color": forms.Select(attrs={"class": "form-select"}),
            "roll_count": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "total_goods_cost": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": 0}),
            "shipping_cost": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": 0}),
            "extra_cost": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": 0}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["color"].queryset = Color.objects.filter(is_active=True).order_by("name")
        can_view_cost = bool(user and user.has_perm("production.view_production_cost"))
        if not can_view_cost:
            for field in ["total_goods_cost", "shipping_cost", "extra_cost"]:
                self.fields.pop(field, None)
        if self.instance and self.instance.pk:
            self.fields["roll_count"].disabled = True
            self.fields["roll_count"].help_text = "Create another receipt to add more physical rolls."


class ProductionProjectForm(forms.ModelForm):
    class Meta:
        model = ProductionProject
        fields = ["finished_item", "color", "expected_qty", "note"]
        widgets = {
            "finished_item": forms.Select(attrs={"class": "form-select"}),
            "color": forms.Select(attrs={"class": "form-select"}),
            "expected_qty": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["finished_item"].queryset = InventoryItem.objects.filter(
            is_active=True,
            item_type=InventoryItem.TYPE_SHIRT,
        ).order_by("sample_style", "code", "name")
        self.fields["color"].queryset = Color.objects.filter(is_active=True).order_by("name")


class CuttingRollUsageForm(forms.ModelForm):
    class Meta:
        model = CuttingRollUsage
        fields = ["roll", "issued_qty", "returned_qty", "note"]
        widgets = {
            "roll": forms.Select(attrs={"class": "form-select"}),
            "issued_qty": forms.NumberInput(attrs={"class": "form-control", "step": "0.001", "min": "0.001"}),
            "returned_qty": forms.NumberInput(attrs={"class": "form-control", "step": "0.001", "min": "0"}),
            "note": forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional note"}),
        }

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        qs = FabricRoll.objects.select_related("receipt", "receipt__color").filter(remaining_qty__gt=0)
        if project:
            qs = qs.filter(receipt__color=project.color).exclude(cutting_usages__project=project)
        self.fields["roll"].queryset = qs.order_by("status", "remaining_qty", "roll_code")
        self.fields["roll"].label_from_instance = lambda roll: (
            f"{roll.roll_code} — {roll.receipt.fabric_name} / {roll.receipt.color.name} — Remaining {roll.remaining_qty}"
        )

    def clean(self):
        data = super().clean()
        roll = data.get("roll")
        issued = Decimal(data.get("issued_qty") or 0)
        returned = Decimal(data.get("returned_qty") or 0)
        if roll and issued > Decimal(roll.remaining_qty or 0):
            self.add_error("issued_qty", "Issued quantity exceeds this roll's remaining quantity.")
        if returned > issued:
            self.add_error("returned_qty", "Returned quantity cannot exceed issued quantity.")
        if roll and self.project and roll.receipt.color_id != self.project.color_id:
            self.add_error("roll", "This roll color does not match the project color.")
        return data


class SewingPartnerForm(forms.ModelForm):
    class Meta:
        model = SewingPartner
        fields = ["name", "phone", "location", "is_active", "note"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "location": forms.TextInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }


class SewingJobForm(forms.ModelForm):
    class Meta:
        model = SewingJob
        fields = ["partner", "sent_date", "expected_return_date", "price_per_piece", "note"]
        widgets = {
            "partner": forms.Select(attrs={"class": "form-select"}),
            "sent_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "expected_return_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "price_per_piece": forms.NumberInput(attrs={"class": "form-control", "step": "0.0001", "min": 0}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["partner"].queryset = SewingPartner.objects.filter(is_active=True).order_by("name")
        if not (user and user.has_perm("production.view_production_cost")):
            self.fields.pop("price_per_piece", None)


class SewingReturnForm(forms.ModelForm):
    class Meta:
        model = SewingReturn
        fields = ["return_date", "note"]
        widgets = {
            "return_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }


class StaffPayableForm(forms.ModelForm):
    class Meta:
        model = ProductionPayable
        fields = ["payee_name", "work_type", "amount", "description"]
        widgets = {
            "payee_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Staff name"}),
            "work_type": forms.TextInput(attrs={"class": "form-control", "placeholder": "Cutting, QC, packing..."}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": 0}),
            "description": forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional note"}),
        }


class PaymentBatchForm(forms.ModelForm):
    class Meta:
        model = ProductionPaymentBatch
        fields = ["payment_date", "payment_method", "reference", "note"]
        widgets = {
            "payment_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "payment_method": forms.Select(attrs={"class": "form-select"}),
            "reference": forms.TextInput(attrs={"class": "form-control", "placeholder": "ABA / bank reference"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
