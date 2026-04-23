from django import forms

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
        fields = ["amount", "note"]
        widgets = {
            "amount": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                }
            ),
            "note": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                }
            ),
        }


class OperatingExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["category", "amount", "note"]
        widgets = {
            "category": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "amount": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                }
            ),
            "note": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                }
            ),
        }


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