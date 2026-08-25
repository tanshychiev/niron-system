from decimal import Decimal

from django import forms
from django.forms import formset_factory

from inventory.models import Color, InventoryItem

from .models import (
    CuttingRollUsage,
    FabricReceipt,
    FabricRoll,
    FabricType,
    ProductionPaymentBatch,
    ProductionPayable,
    ProductionProject,
    ProductionProjectColor,
    ProductionSupplier,
    ProductionExpense,
    SewingJob,
    SewingPartner,
    SewingReturn,
)


class FabricTypeForm(forms.ModelForm):
    class Meta:
        model = FabricType
        fields = ["name", "gsm", "composition", "is_active", "note"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. 250 GSM 100% Cotton"}),
            "gsm": forms.NumberInput(attrs={"class": "form-control", "min": 1, "placeholder": "250"}),
            "composition": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. 100% Cotton"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Optional note"}),
        }


class FabricReceiptForm(forms.ModelForm):
    """
    Fabric receipt form without purchase cost.

    Finance records Goods Cost, Delivery Fee and Extra Cost later from
    Stock In Expense.
    """

    class Meta:
        model = FabricReceipt
        fields = [
            "received_date",
            "supplier_ref",
            "fabric_type",
            "color",
            "roll_count",
            "note",
        ]
        widgets = {
            "received_date": forms.DateInput(
                attrs={
                    "type": "date",
                    "class": "form-control",
                }
            ),
            "supplier_ref": forms.Select(
                attrs={"class": "form-select"}
            ),
            "fabric_type": forms.Select(
                attrs={"class": "form-select"}
            ),
            "color": forms.Select(
                attrs={"class": "form-select"}
            ),
            "roll_count": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": 1,
                }
            ),
            "note": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                }
            ),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

        self.fields["color"].queryset = (
            Color.objects
            .filter(is_active=True)
            .order_by("name")
        )
        self.fields["fabric_type"].queryset = (
            FabricType.objects
            .filter(is_active=True)
            .order_by("name")
        )
        self.fields["supplier_ref"].queryset = (
            ProductionSupplier.objects
            .filter(is_active=True)
            .order_by("name")
        )

        if self.instance and self.instance.pk:
            self.fields["roll_count"].disabled = True
            self.fields["roll_count"].help_text = (
                "Create another receipt to add more physical rolls."
            )

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Fabric Stock In never records purchase cost.
        instance.total_goods_cost = Decimal("0.00")
        instance.shipping_cost = Decimal("0.00")
        instance.extra_cost = Decimal("0.00")

        if commit:
            instance.save()

        return instance


class FabricReceiptHeaderForm(forms.Form):
    received_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"})
    )
    supplier = forms.ModelChoiceField(
        queryset=ProductionSupplier.objects.filter(is_active=True).order_by("name"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class FabricReceiptLineForm(forms.Form):
    fabric_type = forms.ModelChoiceField(
        queryset=FabricType.objects.none(),
        empty_label="Choose fabric type",
        widget=forms.Select(
            attrs={
                "class": "form-select fabric-type-select",
            }
        ),
    )

    color = forms.ModelChoiceField(
        queryset=Color.objects.none(),
        empty_label="Choose colour",
        widget=forms.Select(
            attrs={
                "class": "form-select color-select",
            }
        ),
    )

    roll_count = forms.IntegerField(
        min_value=1,
        initial=1,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control fabric-roll-count",
                "min": 1,
            }
        ),
    )

    # One KG value for every physical roll. The Stock In page keeps this
    # synchronized with roll_count. Stored as comma-separated decimals so we
    # can keep the existing flat formset structure.
    roll_weights = forms.CharField(
        required=True,
        widget=forms.HiddenInput(attrs={"class": "fabric-roll-weights"}),
    )

    note = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Optional note",
            }
        ),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

        self.fields["color"].queryset = (
            Color.objects
            .filter(is_active=True)
            .order_by("name")
        )
        self.fields["fabric_type"].queryset = (
            FabricType.objects
            .filter(is_active=True)
            .order_by("name")
        )

    def clean(self):
        cleaned = super().clean()
        roll_count = int(cleaned.get("roll_count") or 0)
        raw = str(cleaned.get("roll_weights") or "").strip()

        weights = []
        if raw:
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    weight = Decimal(part)
                except Exception:
                    raise forms.ValidationError("Every fabric roll weight must be a valid number.")
                if weight <= 0:
                    raise forms.ValidationError("Every fabric roll weight must be greater than 0 KG.")
                weights.append(weight.quantize(Decimal("0.001")))

        if roll_count > 0 and len(weights) != roll_count:
            raise forms.ValidationError(
                f"Enter the KG for all {roll_count} roll(s). Currently {len(weights)} weight(s) are filled."
            )

        cleaned["roll_weights_list"] = weights
        return cleaned


def fabric_receipt_line_formset(
    *,
    data=None,
    user=None,
    prefix="items",
):
    """
    Fabric Stock In starts with exactly one visible row.

    Additional rows are added only when staff clicks + Add Fabric.
    """
    FormSet = formset_factory(
        FabricReceiptLineForm,
        extra=1,
        can_delete=True,
        min_num=1,
        validate_min=True,
    )

    return FormSet(
        data=data,
        prefix=prefix,
        form_kwargs={"user": user},
    )


class ProductionProjectForm(forms.ModelForm):
    colors = forms.ModelMultipleChoiceField(
        queryset=Color.objects.none(), required=True,
        widget=forms.CheckboxSelectMultiple(),
        help_text="Choose one or more colours. All colours use the same fabric type.",
    )

    class Meta:
        model = ProductionProject
        fields = ["finished_item", "fabric_type", "note"]
        widgets = {
            "finished_item": forms.Select(attrs={"class": "form-select"}),
            "fabric_type": forms.Select(attrs={"class": "form-select"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["finished_item"].queryset = InventoryItem.objects.filter(
            is_active=True, item_type=InventoryItem.TYPE_SHIRT
        ).order_by("sample_style", "code", "name")
        self.fields["fabric_type"].queryset = FabricType.objects.filter(is_active=True).order_by("name")
        self.fields["colors"].queryset = Color.objects.filter(is_active=True).order_by("name")
        if self.instance and self.instance.pk:
            self.fields["colors"].initial = self.instance.project_colors.values_list("color_id", flat=True)


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

    def __init__(self, *args, project=None, project_color=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project
        self.project_color = project_color
        self.fields["issued_qty"].required = False
        self.fields["returned_qty"].required = False
        qs = (
            FabricRoll.objects.select_related("receipt", "receipt__color")
            .filter(remaining_qty__gt=0)
            .exclude(cutting_usages__applied=False)
        )
        if project_color:
            qs = qs.filter(receipt__color=project_color.color).exclude(cutting_usages__project_color=project_color)
        elif project and project.color_id:
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
        if roll and self.project_color and roll.receipt.color_id != self.project_color.color_id:
            self.add_error("roll", "This fabric roll has a different colour from the project.")
        return data


class SewingPartnerForm(forms.ModelForm):
    class Meta:
        model = SewingPartner
        fields = [
            "name",
            "phone",
            "location",
            "is_active",
            "note",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "location": forms.TextInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
            "note": forms.Textarea(
                attrs={"class": "form-control", "rows": 3}
            ),
        }


class SewingJobForm(forms.ModelForm):
    class Meta:
        model = SewingJob
        fields = [
            "project_color", "worker_type", "partner", "staff_name",
            "sent_date", "expected_return_date", "price_per_piece", "note",
        ]
        widgets = {
            "project_color": forms.Select(attrs={"class": "form-select"}),
            "worker_type": forms.Select(attrs={"class": "form-select", "id": "id_worker_type"}),
            "partner": forms.Select(attrs={"class": "form-select", "id": "id_partner"}),
            "staff_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Staff name", "id": "id_staff_name"}),
            "sent_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "expected_return_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "price_per_piece": forms.NumberInput(attrs={"class": "form-control", "step": "0.0001", "min": "0"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, user=None, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = project or getattr(self.instance, "project", None)

        # Set the project on the ModelForm instance before form validation.
        # SewingJob.clean() checks that project_color belongs to project.
        # Without this, a new job has project_id=None during is_valid(),
        # so the form fails with a hidden non-field validation error.
        if self.project and getattr(self.project, "pk", None):
            self.instance.project = self.project

        self.fields["partner"].queryset = SewingPartner.objects.filter(is_active=True).order_by("name")
        if self.project and getattr(self.project, "pk", None):
            self.fields["project_color"].queryset = self.project.project_colors.select_related("color").all()
        else:
            self.fields["project_color"].queryset = ProductionProjectColor.objects.none()
        if user and not user.has_perm("production.view_production_cost"):
            self.fields.pop("price_per_piece", None)

    def clean(self):
        cleaned = super().clean()
        pc = cleaned.get("project_color")
        if pc and self.project and pc.project_id != self.project.id:
            self.add_error("project_color", "This colour does not belong to the project.")
        worker_type = cleaned.get("worker_type")
        if worker_type == SewingJob.WORKER_PARTNER and not cleaned.get("partner"):
            self.add_error("partner", "Choose a sewing partner.")
        if worker_type == SewingJob.WORKER_STAFF and not (cleaned.get("staff_name") or "").strip():
            self.add_error("staff_name", "Enter the staff name.")
        return cleaned


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