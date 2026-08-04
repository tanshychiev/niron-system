from datetime import timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Expense


class ExpenseFilterForm(forms.Form):
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "class": "form-control",
            }
        ),
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "class": "form-control",
            }
        ),
    )
    expense_type = forms.ChoiceField(
        required=False,
        choices=[("", "All Types")] + list(Expense.TYPE_CHOICES),
        widget=forms.Select(
            attrs={
                "class": "form-select",
            }
        ),
    )
    created_by = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Record by who",
            }
        ),
    )
    keyword = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Search note / batch",
            }
        ),
    )


class OtherExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["expense_date", "category", "amount", "note"]
        widgets = {
            "expense_date": forms.DateInput(
                attrs={
                    "class": "form-control",
                    "type": "date",
                }
            ),
            "category": forms.Select(attrs={"class": "form-select"}),
            "amount": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0"}
            ),
            "note": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Example: Grab delivery to supplier",
                }
            ),
        }
        labels = {
            "expense_date": "Expense Date",
            "category": "Expense Category",
            "amount": "Amount",
            "note": "Note",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        today = timezone.localdate()
        minimum_date = today - timedelta(days=30)

        self.fields["category"].choices = Expense.OTHER_CATEGORY_CHOICES
        self.fields["expense_date"].initial = today
        self.fields["expense_date"].widget.attrs.update(
            {
                "min": minimum_date.isoformat(),
                "max": today.isoformat(),
            }
        )

    def clean_expense_date(self):
        selected_date = self.cleaned_data.get("expense_date")
        today = timezone.localdate()
        minimum_date = today - timedelta(days=30)

        if not selected_date:
            raise ValidationError("Expense date is required.")

        if selected_date > today:
            raise ValidationError("Future dates are not allowed.")

        if selected_date < minimum_date:
            raise ValidationError(
                "You can only record an expense from the past 30 days."
            )

        return selected_date


class OperatingExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["category", "amount", "note"]
        widgets = {
            "category": forms.Select(attrs={"class": "form-select"}),
            "amount": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.01", "min": "0"}
            ),
            "note": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Salary, commission, cutting, rent, etc.",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].choices = Expense.OPERATING_CATEGORY_CHOICES


class BatchExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["batch", "note"]
        widgets = {
            "batch": forms.Select(
                attrs={
                    "class": "form-select",
                    "id": "id_batch",
                }
            ),
            "note": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                }
            ),
        }

class BatchExpenseCostForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = [
            "batch_cost",
            "batch_delivery_fee",
            "batch_other_fee",
            "note",
        ]
        labels = {
            "batch_cost": "Goods Cost",
            "batch_delivery_fee": "Delivery Fee",
            "batch_other_fee": "Extra Cost",
        }
        widgets = {
            "batch_cost": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "batch_delivery_fee": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "batch_other_fee": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0"}),
            "note": forms.Textarea(attrs={"class": "form-control", "rows": 4, "placeholder": "Expense note..."}),
        }

    def clean(self):
        cleaned = super().clean()
        for field in ("batch_cost", "batch_delivery_fee", "batch_other_fee"):
            value = cleaned.get(field)
            if value is not None and value < 0:
                self.add_error(field, "Amount cannot be below zero.")
        return cleaned