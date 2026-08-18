from decimal import Decimal

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import Group, Permission, User
from django.db import transaction

from .models import (
    UserProfile,
    StaffSalaryHistory,
    StaffPayrollPayment,
)


ADMIN_GROUP_NAMES = {
    "ADMIN",
    "ADMIN FULL CONTROL",
    "ADMINISTRATOR",
}


def _normalized_role_name(group):
    if not group:
        return ""
    return " ".join((group.name or "").strip().upper().split())


def is_admin_role(group):
    return _normalized_role_name(group) in ADMIN_GROUP_NAMES


def clear_permission_cache(user):
    for cache_name in (
        "_perm_cache",
        "_user_perm_cache",
        "_group_perm_cache",
    ):
        if hasattr(user, cache_name):
            delattr(user, cache_name)


@transaction.atomic
def sync_user_access(user, selected_group):
    """
    Role/Group is the single source of truth for access.

    - One role per user.
    - Old direct user permissions are removed.
    - Admin roles receive every Django permission through the Group.
    - Limited roles receive only permissions checked on their Group.
    - A user moved out of an admin role cannot keep superuser bypass access.
    """
    admin_access = is_admin_role(selected_group)

    user.user_permissions.clear()

    if selected_group:
        user.groups.set([selected_group.pk])
        if admin_access:
            selected_group.permissions.set(Permission.objects.all())
    else:
        user.groups.clear()

    keep_superuser = bool(user.is_superuser and admin_access)
    staff_access = bool(admin_access or keep_superuser)

    User.objects.filter(pk=user.pk).update(
        is_staff=staff_access,
        is_superuser=keep_superuser,
    )

    user.is_staff = staff_access
    user.is_superuser = keep_superuser

    clear_permission_cache(user)
    user.refresh_from_db(fields=["is_staff", "is_superuser"])
    clear_permission_cache(user)


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Enter username",
            }
        )
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Password",
            }
        )
    )


class UserCreateForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Password",
            }
        )
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Confirm Password",
            }
        )
    )
    role = forms.ModelChoiceField(
        queryset=Group.objects.all().order_by("name"),
        required=True,
        empty_label="Select role",
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Role",
        error_messages={
            "required": "Please select a role for this user.",
        },
    )

    class Meta:
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "email",
            "is_active",
        ]
        widgets = {
            "username": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Username"}
            ),
            "first_name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "First name"}
            ),
            "last_name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Last name"}
            ),
            "email": forms.EmailInput(
                attrs={"class": "form-control", "placeholder": "Email"}
            ),
            "is_active": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
        }

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("password") != cleaned_data.get("confirm_password"):
            self.add_error(
                "confirm_password",
                "Password and confirm password do not match.",
            )
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])

        if commit:
            user.save()
            sync_user_access(user, self.cleaned_data.get("role"))
            UserProfile.objects.get_or_create(user=user)

        return user


class UserEditForm(forms.ModelForm):
    new_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "New Password",
                "autocomplete": "new-password",
            }
        ),
    )
    confirm_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Confirm New Password",
                "autocomplete": "new-password",
            }
        ),
    )
    role = forms.ModelChoiceField(
        queryset=Group.objects.all().order_by("name"),
        required=True,
        empty_label="Select role",
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Role",
        error_messages={
            "required": "Please select a role for this user.",
        },
    )

    class Meta:
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "email",
            "is_active",
        ]
        widgets = {
            "username": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Username"}
            ),
            "first_name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "First name"}
            ),
            "last_name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Last name"}
            ),
            "email": forms.EmailInput(
                attrs={"class": "form-control", "placeholder": "Email"}
            ),
            "is_active": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not self.is_bound and self.instance and self.instance.pk:
            current_group = self.instance.groups.order_by("name").first()
            if current_group:
                self.fields["role"].initial = current_group.pk

    def clean(self):
        cleaned_data = super().clean()
        new_password = cleaned_data.get("new_password")
        confirm_password = cleaned_data.get("confirm_password")

        if new_password or confirm_password:
            if new_password != confirm_password:
                self.add_error(
                    "confirm_password",
                    "New password and confirm password do not match.",
                )
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)

        new_password = self.cleaned_data.get("new_password")
        if new_password:
            user.set_password(new_password)

        if commit:
            user.save()
            sync_user_access(user, self.cleaned_data.get("role"))
            UserProfile.objects.get_or_create(user=user)

        return user


class UserProfileForm(forms.ModelForm):
    signature_data = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )

    class Meta:
        model = UserProfile
        fields = [
            "signature",
            "is_staff_employee",
            "join_date",
            "left_date",
            "staff_note",
        ]
        widgets = {
            "signature": forms.FileInput(
                attrs={
                    "class": "signature-file-input",
                    "accept": "image/png,image/jpeg,image/webp",
                }
            ),
            "is_staff_employee": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
            "join_date": forms.DateInput(
                attrs={"type": "date", "class": "form-control"}
            ),
            "left_date": forms.DateInput(
                attrs={"type": "date", "class": "form-control"}
            ),
            "staff_note": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Optional staff note",
                }
            ),
        }
        labels = {
            "signature": "Signature",
            "is_staff_employee": "Include in Staff Payroll",
            "join_date": "Join Date",
            "left_date": "Left Date",
            "staff_note": "Staff Note",
        }

    def clean(self):
        cleaned_data = super().clean()
        is_staff_employee = cleaned_data.get("is_staff_employee")
        join_date = cleaned_data.get("join_date")
        left_date = cleaned_data.get("left_date")

        if is_staff_employee and not join_date:
            self.add_error(
                "join_date",
                "Join date is required for staff payroll.",
            )

        if join_date and left_date and left_date < join_date:
            self.add_error(
                "left_date",
                "Left date cannot be earlier than join date.",
            )

        return cleaned_data


class StaffSalaryHistoryForm(forms.ModelForm):
    class Meta:
        model = StaffSalaryHistory
        fields = ["salary", "effective_date", "note"]
        widgets = {
            "salary": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "step": "0.01",
                    "min": "0",
                    "placeholder": "250.00",
                }
            ),
            "effective_date": forms.DateInput(
                attrs={"type": "date", "class": "form-control"}
            ),
            "note": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Starting salary / salary upgrade",
                }
            ),
        }
        labels = {
            "salary": "Base Salary",
            "effective_date": "Effective Date",
            "note": "Note",
        }


class StaffFirstPaymentForm(forms.Form):
    amount = forms.DecimalField(
        label="First Payment Amount",
        min_value=Decimal("0.01"),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0.01",
                "placeholder": "130.00",
            }
        ),
    )
    payment_date = forms.DateField(
        label="Payment Date",
        widget=forms.DateInput(
            attrs={"type": "date", "class": "form-control"}
        ),
    )
    payment_method = forms.ChoiceField(
        label="Payment Method",
        choices=StaffPayrollPayment.METHOD_CHOICES,
        initial=StaffPayrollPayment.METHOD_CASH,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reference = forms.CharField(
        label="Reference",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "ABA / bank reference",
            }
        ),
    )
    note = forms.CharField(
        label="Note",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Optional note",
            }
        ),
    )


class StaffFinalPaymentForm(forms.Form):
    commission = forms.DecimalField(
        label="Commission",
        required=False,
        min_value=Decimal("0.00"),
        max_digits=12,
        decimal_places=2,
        initial=Decimal("0.00"),
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0",
                "placeholder": "0.00",
            }
        ),
    )
    bonus = forms.DecimalField(
        label="Bonus",
        required=False,
        min_value=Decimal("0.00"),
        max_digits=12,
        decimal_places=2,
        initial=Decimal("0.00"),
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0",
                "placeholder": "0.00",
            }
        ),
    )
    deduction = forms.DecimalField(
        label="Deduction",
        required=False,
        min_value=Decimal("0.00"),
        max_digits=12,
        decimal_places=2,
        initial=Decimal("0.00"),
        widget=forms.NumberInput(
            attrs={
                "class": "form-control",
                "step": "0.01",
                "min": "0",
                "placeholder": "0.00",
            }
        ),
    )
    payment_date = forms.DateField(
        label="Payment Date",
        widget=forms.DateInput(
            attrs={"type": "date", "class": "form-control"}
        ),
    )
    payment_method = forms.ChoiceField(
        label="Payment Method",
        choices=StaffPayrollPayment.METHOD_CHOICES,
        initial=StaffPayrollPayment.METHOD_CASH,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reference = forms.CharField(
        label="Reference",
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "ABA / bank reference",
            }
        ),
    )
    note = forms.CharField(
        label="Note",
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Optional note",
            }
        ),
    )

    def clean(self):
        cleaned = super().clean()
        cleaned["commission"] = cleaned.get("commission") or Decimal("0.00")
        cleaned["bonus"] = cleaned.get("bonus") or Decimal("0.00")
        cleaned["deduction"] = cleaned.get("deduction") or Decimal("0.00")
        return cleaned


class RoleForm(forms.ModelForm):
    permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.select_related("content_type").order_by(
            "content_type__app_label",
            "codename",
        ),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = Group
        fields = ["name", "permissions"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. Operator, Admin, Staff...",
                }
            )
        }

    def save(self, commit=True):
        role = super().save(commit=commit)

        if not commit:
            return role

        if is_admin_role(role):
            role.permissions.set(Permission.objects.all())

        for user in role.user_set.all():
            sync_user_access(user, role)

        return role
