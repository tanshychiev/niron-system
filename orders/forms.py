from django import forms
from django.forms import inlineformset_factory

from inventory.models import Color, InventoryItem, Size
from .models import Order, OrderItem


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        if not data:
            return []
        if not isinstance(data, (list, tuple)):
            data = [data]

        cleaned = []
        for item in data:
            cleaned.append(super().clean(item, initial))
        return cleaned


class OrderForm(forms.ModelForm):
    deadline = forms.DateTimeField(
        widget=forms.DateTimeInput(
            attrs={
                "type": "datetime-local",
                "class": "form-control",
            }
        )
    )

    design_files = MultipleFileField(
        required=False,
        widget=MultipleFileInput(
            attrs={
                "accept": "image/*",
                "class": "form-control",
            }
        ),
    )

    shipping_fee = forms.DecimalField(
        required=False,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "placeholder": "0.00",
            }
        ),
    )

    discount_amount = forms.DecimalField(
        required=False,
        initial=0,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "placeholder": "0.00",
            }
        ),
    )

    class Meta:
        model = Order
        fields = [
            "order_type",
            "customer_name",
            "phone",
            "customer_location",
            "deadline",
            "remark",
        ]
        widgets = {
            "order_type": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "customer_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Customer name",
                }
            ),
            "phone": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Phone",
                }
            ),
            "customer_location": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Location",
                }
            ),
            "remark": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Remark",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["customer_location"].required = False
        self.fields["remark"].required = False
        self.fields["phone"].required = False


class OrderItemForm(forms.ModelForm):
    description = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Description",
            }
        ),
    )

    class Meta:
        model = OrderItem
        fields = [
            "item_mode",
            "description",
            "shirt_item",
            "color",
            "size",
            "film_item",
            "quantity",
            "unit_price",
            "film_meter_per_piece",
            "manual_film_meter",
        ]
        widgets = {
            "item_mode": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "shirt_item": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "color": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "size": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "film_item": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "quantity": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Qty",
                    "min": "0",
                }
            ),
            "unit_price": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "0.00",
                    "step": "0.01",
                    "min": "0",
                }
            ),
            "film_meter_per_piece": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Meter / piece",
                    "step": "0.0001",
                    "min": "0",
                }
            ),
            "manual_film_meter": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Film meter",
                    "step": "0.0001",
                    "min": "0",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["shirt_item"].queryset = InventoryItem.objects.filter(
            item_type=InventoryItem.TYPE_SHIRT,
            is_active=True,
        ).order_by("code", "name")

        self.fields["film_item"].queryset = InventoryItem.objects.filter(
            item_type=InventoryItem.TYPE_FILM,
            is_active=True,
        ).order_by("code", "name")

        self.fields["color"].queryset = Color.objects.filter(
            is_active=True
        ).order_by("name")

        self.fields["size"].queryset = Size.objects.filter(
            is_active=True
        ).order_by("sort_order", "id")

        self.fields["quantity"].required = False
        self.fields["unit_price"].required = False
        self.fields["film_meter_per_piece"].required = False
        self.fields["manual_film_meter"].required = False

        self.fields["shirt_item"].required = False
        self.fields["film_item"].required = False
        self.fields["color"].required = False
        self.fields["size"].required = False


OrderItemFormSet = inlineformset_factory(
    Order,
    OrderItem,
    form=OrderItemForm,
    extra=1,
    can_delete=True,
)
class ProductionFilterForm(forms.Form):
    STATUS_ACTIVE = "ACTIVE"
    STATUS_ALL = "ALL"
    STATUS_DONE = "DONE"
    STATUS_CANCEL = "CANCEL"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Pending & Processing"),
        (STATUS_ALL, "All"),
        (STATUS_DONE, "Done"),
        (STATUS_CANCEL, "Cancel"),
    ]

    q = forms.CharField(
        required=False,
        label="Search",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Customer name or order no",
            }
        ),
    )

    status = forms.ChoiceField(
        required=False,
        label="Status",
        initial=STATUS_ACTIVE,
        choices=STATUS_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    deadline = forms.DateField(
        required=False,
        label="Deadline",
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "class": "form-control",
            }
        ),
    )