from django import forms
from django.forms import inlineformset_factory

from inventory.models import Color, InventoryItem, Size
from .models import Order, OrderDesign, OrderItem


# =========================
# MULTIPLE FILE SUPPORT
# =========================
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


# =========================
# ORDER FORM (FIXED)
# =========================
class OrderForm(forms.ModelForm):

    # ✅ FIX: DATE ONLY (NO TIME)
    deadline = forms.DateField(
        widget=forms.DateInput(
            attrs={
                "type": "date",
                "class": "form-control",
            }
        )
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
            "service_type",
            "customer_name",
            "phone",
            "customer_location",
            "deadline",
            "remark",
        ]
        widgets = {
            "order_type": forms.Select(attrs={"class": "form-select"}),
            "service_type": forms.Select(
                attrs={"class": "form-select", "id": "id_service_type"}
            ),
            "customer_name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Customer name"}
            ),
            "phone": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Phone"}
            ),
            "customer_location": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Location"}
            ),
            "remark": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Remark"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["customer_location"].required = False
        self.fields["remark"].required = False
        self.fields["phone"].required = False


# =========================
# DESIGN FORM
# =========================
class OrderDesignForm(forms.ModelForm):
    design_files = MultipleFileField(
        required=False,
        widget=MultipleFileInput(
            attrs={
                "accept": "image/*",
                "class": "form-control",
            }
        ),
    )

    class Meta:
        model = OrderDesign
        fields = ["name", "remark"]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Design name"}
            ),
            "remark": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Design remark"}
            ),
        }


# =========================
# ORDER ITEM FORM
# =========================
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
            "description",
            "shirt_item",
            "color",
            "size",
            "film_item",
            "film_meter",
            "quantity",
            "unit_price",
        ]
        widgets = {
            "shirt_item": forms.Select(attrs={"class": "form-select shirt-select"}),
            "color": forms.Select(attrs={"class": "form-select color-select"}),
            "size": forms.Select(attrs={"class": "form-select size-select"}),
            "film_item": forms.Select(attrs={"class": "form-select film-select"}),
            "film_meter": forms.NumberInput(
                attrs={
                    "class": "form-control film-meter-input",
                    "placeholder": "Film meter",
                    "step": "0.0001",
                    "min": "0",
                }
            ),
            "quantity": forms.NumberInput(
                attrs={
                    "class": "form-control qty-input",
                    "placeholder": "Qty",
                    "step": "1",
                    "min": "0",
                }
            ),
            "unit_price": forms.NumberInput(
                attrs={
                    "class": "form-control unit-price-input",
                    "placeholder": "0.00",
                    "step": "0.01",
                    "min": "0",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        self.order = kwargs.pop("order", None)
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

        # all optional (logic handled in clean)
        for name in [
            "shirt_item",
            "color",
            "size",
            "film_item",
            "film_meter",
            "quantity",
            "unit_price",
        ]:
            self.fields[name].required = False

    def clean(self):
        cleaned = super().clean()

        service_type = None
        if self.order:
            service_type = self.order.service_type
        elif self.instance and self.instance.order_id:
            service_type = self.instance.order.service_type

        shirt_item = cleaned.get("shirt_item")
        color = cleaned.get("color")
        size = cleaned.get("size")
        film_item = cleaned.get("film_item")
        film_meter = cleaned.get("film_meter")

        if service_type == Order.SERVICE_FULL:
            if not shirt_item:
                self.add_error("shirt_item", "Please choose shirt item.")
            if not color:
                self.add_error("color", "Please choose color.")
            if not size:
                self.add_error("size", "Please choose size.")

        elif service_type == Order.SERVICE_FILM_ONLY:
            if not film_item:
                self.add_error("film_item", "Please choose film item.")
            if not film_meter or film_meter <= 0:
                self.add_error("film_meter", "Please enter film meter.")

        return cleaned


# =========================
# FORMSET
# =========================
OrderItemFormSet = inlineformset_factory(
    OrderDesign,
    OrderItem,
    form=OrderItemForm,
    extra=1,
    can_delete=True,
)


# =========================
# FILTER FORM
# =========================
class ProductionFilterForm(forms.Form):
    STATUS_ACTIVE = "ACTIVE"
    STATUS_ALL = "ALL"
    STATUS_DONE = "DONE"
    STATUS_CANCEL = "CANCEL"

    SORT_DEADLINE_ASC = "deadline_asc"
    SORT_DEADLINE_DESC = "deadline_desc"
    SORT_CREATED_DESC = "created_desc"
    SORT_CREATED_ASC = "created_asc"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Pending & Processing"),
        (STATUS_ALL, "All"),
        (STATUS_DONE, "Done"),
        (STATUS_CANCEL, "Cancel"),
    ]

    SORT_CHOICES = [
        (SORT_DEADLINE_ASC, "Deadline nearest first"),
        (SORT_DEADLINE_DESC, "Deadline latest first"),
        (SORT_CREATED_DESC, "Created newest first"),
        (SORT_CREATED_ASC, "Created oldest first"),
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

    sort = forms.ChoiceField(
        required=False,
        label="Sort",
        initial=SORT_DEADLINE_ASC,
        choices=SORT_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )