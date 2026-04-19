from decimal import Decimal

from django import forms
from django.forms import inlineformset_factory

from .models import (
    Color,
    InventoryAdjustment,
    InventoryBatch,
    InventoryBatchItem,
    InventoryItem,
    Size,
)


class InventoryItemForm(forms.ModelForm):
    class Meta:
        model = InventoryItem
        fields = ["name", "sample_style", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Item name"}),
            "sample_style": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.item_type = InventoryItem.TYPE_SHIRT
        instance.unit = InventoryItem.UNIT_PCS
        if commit:
            instance.save()
        return instance


class ColorForm(forms.ModelForm):
    class Meta:
        model = Color
        fields = ["name", "hex_code", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Color name"}),
            "hex_code": forms.TextInput(attrs={"class": "form-control", "type": "color", "value": "#000000"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_hex_code(self):
        value = (self.cleaned_data.get("hex_code") or "").strip().upper()
        if not value:
            value = "#000000"
        if not value.startswith("#"):
            value = f"#{value}"
        if len(value) != 7:
            raise forms.ValidationError("Hex code must be in format like #000000")
        return value

    def save(self, commit=True):
        instance = super().save(commit=False)

        if not instance.code:
            name = (instance.name or "").strip().upper()
            words = [w for w in name.split() if w]
            base = "".join(words)[:3] or "COL"
            code = base
            i = 1

            while Color.objects.filter(code=code).exclude(pk=instance.pk).exists():
                code = f"{base}{i}"
                i += 1

            instance.code = code

        if commit:
            instance.save()
        return instance


class SizeForm(forms.ModelForm):
    class Meta:
        model = Size
        fields = ["code", "name", "sort_order", "is_active"]
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-control", "placeholder": "Size code"}),
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Size name"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class InventoryBatchForm(forms.ModelForm):
    received_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"})
    )

    class Meta:
        model = InventoryBatch
        fields = [
            "received_date",
            "supplier",
            "note",
        ]
        widgets = {
            "supplier": forms.TextInput(attrs={"class": "form-control", "placeholder": "Supplier"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "Note"}),
        }

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.status = InventoryBatch.STATUS_FINAL

        if not instance.batch_no:
            date_part = instance.received_date.strftime("%Y%m%d") if instance.received_date else "BATCH"
            base = f"STK-{date_part}"
            batch_no = base
            i = 1
            while InventoryBatch.objects.filter(batch_no=batch_no).exclude(pk=instance.pk).exists():
                batch_no = f"{base}-{i}"
                i += 1
            instance.batch_no = batch_no

        if commit:
            instance.save()
        return instance

class InventoryBatchItemForm(forms.ModelForm):
    quantity = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
    )

    class Meta:
        model = InventoryBatchItem
        fields = ["item", "color", "size", "quantity"]
        widgets = {
            "item": forms.Select(attrs={"class": "form-select"}),
            "color": forms.Select(attrs={"class": "form-select"}),
            "size": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["item"].queryset = InventoryItem.objects.filter(
            is_active=True,
            item_type=InventoryItem.TYPE_SHIRT,
        ).order_by("code")
        self.fields["color"].queryset = Color.objects.filter(is_active=True).order_by("name")
        self.fields["size"].queryset = Size.objects.filter(is_active=True).order_by("sort_order", "id")

        if self.instance and self.instance.pk:
            self.fields["quantity"].initial = self.instance.qty_received

    def save(self, commit=True):
        instance = super().save(commit=False)
        qty = self.cleaned_data.get("quantity") or Decimal("0")

        if instance.pk and not instance.can_edit_received_qty:
            if qty != instance.qty_received:
                raise forms.ValidationError(
                    "This row already has stock used. Please use stock adjustment instead of editing qty."
                )

        if not instance.pk:
            instance.qty_received = qty
            instance.qty_remaining = qty
        else:
            old_received = instance.qty_received or Decimal("0")
            diff = qty - old_received
            instance.qty_received = qty
            instance.qty_remaining = (instance.qty_remaining or Decimal("0")) + diff
            if instance.qty_remaining < 0:
                raise forms.ValidationError("Remaining qty cannot go below 0.")

        if commit:
            instance.save()
        return instance


class InventoryAdjustmentForm(forms.ModelForm):
    stocktake_final_qty = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
    )

    class Meta:
        model = InventoryAdjustment
        fields = [
            "adjustment_type",
            "qty",
            "stocktake_final_qty",
            "reason",
        ]
        widgets = {
            "adjustment_type": forms.Select(attrs={"class": "form-select"}),
            "qty": forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
            "reason": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        self.batch_item = kwargs.pop("batch_item", None)
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        adjustment_type = cleaned_data.get("adjustment_type")
        qty = cleaned_data.get("qty")
        stocktake_final_qty = cleaned_data.get("stocktake_final_qty")

        if adjustment_type == InventoryAdjustment.TYPE_STOCKTAKE:
            if stocktake_final_qty is None:
                raise forms.ValidationError("Please enter the final counted qty for stock take.")
            if stocktake_final_qty < 0:
                raise forms.ValidationError("Final counted qty cannot be below 0.")
        else:
            if qty is None or qty <= 0:
                raise forms.ValidationError("Qty must be greater than 0.")

            old_qty = self.batch_item.qty_remaining if self.batch_item else Decimal("0")
            if adjustment_type in [
                InventoryAdjustment.TYPE_REMOVE,
                InventoryAdjustment.TYPE_DAMAGE,
                InventoryAdjustment.TYPE_LOST,
            ] and qty > old_qty:
                raise forms.ValidationError("Cannot reduce more than remaining stock.")

        return cleaned_data


class InventoryAdjustStockSelectForm(forms.Form):
    item = forms.ModelChoiceField(
        queryset=InventoryItem.objects.filter(
            is_active=True,
            item_type=InventoryItem.TYPE_SHIRT,
        ).order_by("code"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    color = forms.ModelChoiceField(
        queryset=Color.objects.filter(is_active=True).order_by("name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    size = forms.ModelChoiceField(
        queryset=Size.objects.filter(is_active=True).order_by("sort_order", "id"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )


InventoryBatchItemFormSet = inlineformset_factory(
    InventoryBatch,
    InventoryBatchItem,
    form=InventoryBatchItemForm,
    extra=1,
    can_delete=True,
)

class InventoryAdjustStockSelectForm(forms.Form):
    item = forms.ModelChoiceField(
        queryset=InventoryItem.objects.filter(
            is_active=True,
            item_type=InventoryItem.TYPE_SHIRT,
        ).order_by("code"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    color = forms.ModelChoiceField(
        queryset=Color.objects.filter(is_active=True).order_by("name"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    size = forms.ModelChoiceField(
        queryset=Size.objects.filter(is_active=True).order_by("sort_order", "id"),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class InventoryAdjustVariantForm(forms.Form):
    adjustment_type = forms.ChoiceField(
        choices=[
            ("STOCKTAKE", "Stock Take"),
            ("LOST", "Lost"),
            ("DAMAGE", "Damage"),
            ("FOUND", "Found Extra"),
            ("ADD", "Add"),
            ("REMOVE", "Remove"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    qty = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
    )
    final_qty = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01"}),
    )
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )

    def clean(self):
        cleaned_data = super().clean()
        adjustment_type = cleaned_data.get("adjustment_type")
        qty = cleaned_data.get("qty")
        final_qty = cleaned_data.get("final_qty")

        if adjustment_type == "STOCKTAKE":
            if final_qty is None:
                raise forms.ValidationError("Please enter final total qty.")
            if final_qty < 0:
                raise forms.ValidationError("Final total qty cannot be below 0.")
        else:
            if qty is None or qty <= 0:
                raise forms.ValidationError("Qty must be greater than 0.")

        return cleaned_data