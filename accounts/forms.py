from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import Group, Permission, User

from .models import UserProfile


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
    """
    Remove Django's cached permission values from the current User object.
    This makes a changed role take effect immediately in the current process.
    """
    for cache_name in (
        "_perm_cache",
        "_user_perm_cache",
        "_group_perm_cache",
    ):
        if hasattr(user, cache_name):
            delattr(user, cache_name)


def sync_user_access(user, selected_group):
    """
    Make the selected role the single source of truth for user access.

    Admin roles:
    - receive every Django permission
    - are marked is_staff=True
    - are marked is_superuser=True

    Other roles:
    - receive only permissions selected on their Group/Role
    - are not staff or superusers
    """
    user.groups.clear()

    if selected_group:
        if is_admin_role(selected_group):
            selected_group.permissions.set(Permission.objects.all())

        user.groups.add(selected_group)

    admin_access = is_admin_role(selected_group)

    user.is_staff = admin_access
    user.is_superuser = admin_access
    user.save(update_fields=["is_staff", "is_superuser"])

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
                "placeholder": "Enter password",
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
    groups = forms.ModelChoiceField(
        queryset=Group.objects.all().order_by("name"),
        required=False,
        empty_label="Select role",
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Role",
    )

    class Meta:
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "email",
            "is_active",
            "groups",
        ]
        widgets = {
            "username": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Username",
                }
            ),
            "first_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "First name",
                }
            ),
            "last_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Last name",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Email",
                }
            ),
            "is_active": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
        }

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password != confirm_password:
            raise forms.ValidationError(
                "Password and confirm password do not match."
            )

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])

        if commit:
            user.save()

            selected_group = self.cleaned_data.get("groups")
            sync_user_access(user, selected_group)

            UserProfile.objects.get_or_create(user=user)

        return user


class UserEditForm(forms.ModelForm):
    new_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "New Password",
            }
        ),
    )
    confirm_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "placeholder": "Confirm New Password",
            }
        ),
    )
    groups = forms.ModelChoiceField(
        queryset=Group.objects.all().order_by("name"),
        required=False,
        empty_label="Select role",
        widget=forms.Select(attrs={"class": "form-select"}),
        label="Role",
    )

    class Meta:
        model = User
        fields = [
            "username",
            "first_name",
            "last_name",
            "email",
            "is_active",
            "groups",
        ]
        widgets = {
            "username": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Username",
                }
            ),
            "first_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "First name",
                }
            ),
            "last_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Last name",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Email",
                }
            ),
            "is_active": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk:
            first_group = self.instance.groups.order_by("name").first()
            if first_group:
                self.fields["groups"].initial = first_group

    def clean(self):
        cleaned_data = super().clean()
        new_password = cleaned_data.get("new_password")
        confirm_password = cleaned_data.get("confirm_password")

        if new_password or confirm_password:
            if new_password != confirm_password:
                raise forms.ValidationError(
                    "New password and confirm password do not match."
                )

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)

        new_password = self.cleaned_data.get("new_password")
        if new_password:
            user.set_password(new_password)

        if commit:
            user.save()

            selected_group = self.cleaned_data.get("groups")
            sync_user_access(user, selected_group)

            UserProfile.objects.get_or_create(user=user)

        return user


class UserProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = ["signature"]
        widgets = {
            "signature": forms.ClearableFileInput(
                attrs={
                    "class": "form-control",
                    "accept": "image/*",
                }
            )
        }


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

        # Admin roles always have full permission without requiring every
        # checkbox to be selected manually.
        if is_admin_role(role):
            role.permissions.set(Permission.objects.all())

        # Re-sync all existing users of this role after role name or
        # permissions are changed.
        for user in role.user_set.all():
            sync_user_access(user, role)

        return role