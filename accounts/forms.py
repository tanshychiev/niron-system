from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import Group, Permission, User


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
        widget=forms.Select(
            attrs={
                "class": "form-select",
            }
        ),
        label="Role",
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_active", "groups"]
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control", "placeholder": "Username"}),
            "first_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "First name"}),
            "last_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Last name"}),
            "email": forms.EmailInput(attrs={"class": "form-control", "placeholder": "Email"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")

        if password != confirm_password:
            raise forms.ValidationError("Password and confirm password do not match.")

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])

        if commit:
            user.save()

            selected_group = self.cleaned_data.get("groups")
            user.groups.clear()
            if selected_group:
                user.groups.add(selected_group)

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
        widget=forms.Select(
            attrs={
                "class": "form-select",
            }
        ),
        label="Role",
    )

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "is_active", "groups"]
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control", "placeholder": "Username"}),
            "first_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "First name"}),
            "last_name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Last name"}),
            "email": forms.EmailInput(attrs={"class": "form-control", "placeholder": "Email"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
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
                raise forms.ValidationError("New password and confirm password do not match.")

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)

        new_password = self.cleaned_data.get("new_password")
        if new_password:
            user.set_password(new_password)

        if commit:
            user.save()

            selected_group = self.cleaned_data.get("groups")
            user.groups.clear()
            if selected_group:
                user.groups.add(selected_group)

        return user


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