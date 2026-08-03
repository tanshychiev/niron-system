from decimal import Decimal

from django import forms
from django.forms import formset_factory

from inventory.models import Color, InventoryItem

from .models import (
    CuttingRollUsage,
    FabricReceipt,
    FabricRoll,
    ProductionPaymentBatch,
    ProductionPayable,
    ProductionProject,
    ProductionSupplier,
    ProductionExpense,
    SewingJob,
    SewingPartner,
    SewingReturn,
)


class FabricReceiptForm(forms.ModelForm):
    class Meta:
        model = FabricReceipt
        fields = [
            "received_date",
            "supplier_ref",
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
            "supplier_ref": forms.Select(attrs={"class": "form-select"}),
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
        self.fields["supplier_ref"].queryset = ProductionSupplier.objects.filter(is_active=True).order_by("name")
        # Cost entry is optional and available to stock-in staff.
        # Finance can review or correct it later.
        if self.instance and self.instance.pk:
            self.fields["roll_count"].disabled = True
            self.fields["roll_count"].help_text = "Create another receipt to add more physical rolls."


class FabricReceiptHeaderForm(forms.Form):
    received_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"})
    )
    supplier = forms.ModelChoiceField(
        queryset=ProductionSupplier.objects.filter(is_active=True).order_by("name"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class FabricReceiptLineForm(forms.Form):
    fabric_name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Cotton 220 GSM"}),
    )
    color = forms.ModelChoiceField(
        queryset=Color.objects.none(),
        widget=forms.Select(attrs={"class": "form-select color-select"}),
    )
    roll_count = forms.IntegerField(
        min_value=1,
        initial=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": 1}),
    )
    total_goods_cost = forms.DecimalField(
        min_value=Decimal("0"),
        decimal_places=2,
        max_digits=14,
        initial=Decimal("0"),
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": 0}),
    )
    shipping_cost = forms.DecimalField(
        min_value=Decimal("0"),
        decimal_places=2,
        max_digits=14,
        initial=Decimal("0"),
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": 0}),
    )
    extra_cost = forms.DecimalField(
        min_value=Decimal("0"),
        decimal_places=2,
        max_digits=14,
        initial=Decimal("0"),
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": 0}),
    )
    note = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional note"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["color"].queryset = Color.objects.filter(is_active=True).order_by("name")
        # Cost entry is optional and available to stock-in staff.
        # Finance can review or correct it later.


def fabric_receipt_line_formset(*, data=None, user=None, prefix="items"):
    FormSet = formset_factory(FabricReceiptLineForm, extra=1, can_delete=True, min_num=1, validate_min=True)
    formset = FormSet(data=data, prefix=prefix, form_kwargs={"user": user})
    return formset


class ProductionProjectForm(forms.ModelForm):
    class Meta:
        model = ProductionProject
        fields = ["finished_item", "color", "note"]
        widgets = {
            "finished_item": forms.Select(attrs={"class": "form-select"}),
            "color": forms.Select(attrs={"class": "form-select"}),
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
            "issued_qty": forms.HiddenInput(),
            "returned_qty": forms.HiddenInput(),
            "note": forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional note"}),
        }

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.fields["issued_qty"].required = False
        self.fields["returned_qty"].required = False
        qs = (
            FabricRoll.objects.select_related("receipt", "receipt__color")
            .filter(remaining_qty__gt=0)
            .exclude(cutting_usages__applied=False)
        )
        if project:
            qs = qs.filter(receipt__color=project.color).exclude(cutting_usages__project=project)
        self.fields["roll"].queryset = qs.order_by("status", "remaining_qty", "roll_code")
        self.fields["roll"].label_from_instance = lambda roll: (
            f"{roll.roll_code} — {roll.receipt.fabric_name} / {roll.receipt.color.name} — Available {roll.available_qty}"
        )

    def clean(self):
        data = super().clean()
        roll = data.get("roll")
        if roll:
            # Reserving a roll holds all of its currently available quantity.
            # The unused quantity is entered later when cutting is confirmed.
            data["issued_qty"] = Decimal(roll.available_qty or 0)
            data["returned_qty"] = Decimal("0")
        if roll and self.project and roll.receipt.color_id != self.project.color_id:
            self.add_error("roll", "This fabric roll has a different colour from the project.")
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
        fields = ["partner", "sent_date", "expected_return_date", "note"]
        widgets = {
            "partner": forms.Select(attrs={"class": "form-select"}),
            "sent_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "expected_return_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["partner"].queryset = SewingPartner.objects.filter(is_active=True).order_by("name")


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


class ProductionSupplierForm(forms.ModelForm):
    class Meta:
        model = ProductionSupplier
        fields = ["name", "phone", "location", "is_active", "note"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "autofocus": True}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "location": forms.TextInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }


class ProductionExpenseForm(forms.ModelForm):
    class Meta:
        model = ProductionExpense
        fields = ["expense_date", "category", "supplier", "sewing_partner", "project", "amount", "payment_method", "reference", "note"]
        widgets = {
            "expense_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "supplier": forms.Select(attrs={"class": "form-select"}),
            "sewing_partner": forms.Select(attrs={"class": "form-select"}),
            "project": forms.Select(attrs={"class": "form-select"}),
            "amount": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "payment_method": forms.TextInput(attrs={"class": "form-control", "placeholder": "Cash / ABA / Bank"}),
            "reference": forms.TextInput(attrs={"class": "form-control"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["supplier"].queryset = ProductionSupplier.objects.filter(is_active=True).order_by("name")
        self.fields["sewing_partner"].queryset = SewingPartner.objects.filter(is_active=True).order_by("name")
        self.fields["project"].queryset = ProductionProject.objects.order_by("-created_at")