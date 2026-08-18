import base64
import binascii
from collections import defaultdict
from io import BytesIO

from PIL import Image

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import (
    LoginForm,
    RoleForm,
    UserCreateForm,
    UserEditForm,
    UserProfileForm,
)
from .models import UserProfile


MAX_SIGNATURE_BYTES = 5 * 1024 * 1024
ALLOWED_SIGNATURE_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


def _form_error_messages(*forms):
    errors = []

    for form in forms:
        if not form:
            continue

        for field_name, field_errors in form.errors.items():
            if field_name == "__all__":
                label = "Form"
            else:
                field = form.fields.get(field_name)
                label = field.label if field else field_name.replace("_", " ").title()

            for error in field_errors:
                errors.append(f"{label}: {error}")

    return errors


def _signature_preview_data_url(profile):
    """Return the current signature as inline base64 so preview works even
    when /media/ is not being served correctly by nginx/Django.
    """
    if not profile or not profile.signature:
        return ""

    try:
        name = profile.signature.name
        lower = name.lower()
        if lower.endswith(".png"):
            mime = "image/png"
        elif lower.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif lower.endswith(".webp"):
            mime = "image/webp"
        else:
            mime = "image/png"

        with profile.signature.storage.open(name, "rb") as fh:
            raw = fh.read()

        if not raw:
            return ""

        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    except Exception:
        return ""


def _decode_signature_data_url(data_url):
    if not data_url:
        return None

    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        raise ValidationError("Signature image data is invalid.")

    try:
        header, payload = data_url.split(",", 1)
    except ValueError:
        raise ValidationError("Signature image data is incomplete.")

    if ";base64" not in header:
        raise ValidationError("Signature image must be base64 encoded.")

    mime = header[5:].split(";", 1)[0].lower().strip()
    extension = ALLOWED_SIGNATURE_MIME.get(mime)

    if not extension:
        raise ValidationError("Signature must be PNG, JPG/JPEG, or WEBP.")

    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("Signature image could not be decoded.")

    if not raw:
        raise ValidationError("Signature image is empty.")

    if len(raw) > MAX_SIGNATURE_BYTES:
        raise ValidationError("Signature image is too large. Maximum size is 5 MB.")

    try:
        image = Image.open(BytesIO(raw))
        image.verify()
    except Exception:
        raise ValidationError("Signature file is not a valid image.")

    return raw, extension


def _replace_signature(profile, decoded_signature):
    if not decoded_signature:
        return

    raw, extension = decoded_signature

    if profile.signature:
        try:
            profile.signature.delete(save=False)
        except Exception:
            pass

    stamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    filename = f"signature_{profile.user_id}_{stamp}.{extension}"
    profile.signature.save(filename, ContentFile(raw), save=True)


def _remove_signature(profile):
    if profile.signature:
        try:
            profile.signature.delete(save=False)
        except Exception:
            pass
        profile.signature = None
        profile.save(update_fields=["signature"])


def _user_form_context(
    *,
    form,
    profile_form,
    profile,
    page_title,
    submit_label,
    user_obj=None,
    errors=None,
    page_alert="",
):
    return {
        "form": form,
        "profile_form": profile_form,
        "profile": profile,
        "user_obj": user_obj,
        "page_title": page_title,
        "submit_label": submit_label,
        "signature_preview": _signature_preview_data_url(profile),
        "form_error_messages": errors or [],
        "page_alert": page_alert,
    }


def login_view(request):
    if request.user.is_authenticated:
        return redirect("inventory_list")

    form = LoginForm(request, data=request.POST or None)

    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        login(request, user)
        messages.success(request, "Login successful.")
        return redirect("inventory_list")

    return render(request, "accounts/login.html", {"form": form})


@login_required
def logout_view(request):
    logout(request)
    messages.success(request, "Logged out successfully.")
    return redirect("login")


@login_required
@permission_required("auth.view_user", raise_exception=True)
def user_list(request):
    users = User.objects.prefetch_related("groups").order_by("username")
    return render(request, "accounts/user_list.html", {"users": users})


@login_required
@permission_required("auth.add_user", raise_exception=True)
def user_create(request):
    empty_profile = None

    if request.method == "POST":
        form = UserCreateForm(request.POST)
        profile_form = UserProfileForm(request.POST, request.FILES)

        decoded_signature = None
        signature_error = None

        if profile_form.is_valid():
            try:
                decoded_signature = _decode_signature_data_url(
                    profile_form.cleaned_data.get("signature_data") or ""
                )
            except ValidationError as exc:
                signature_error = str(exc.message)
                profile_form.add_error("signature_data", signature_error)

        if form.is_valid() and profile_form.is_valid() and not signature_error:
            try:
                with transaction.atomic():
                    user_obj = form.save()
                    profile, _ = UserProfile.objects.get_or_create(user=user_obj)

                    if decoded_signature:
                        _replace_signature(profile, decoded_signature)
                    elif profile_form.cleaned_data.get("signature"):
                        profile.signature = profile_form.cleaned_data["signature"]
                        profile.save(update_fields=["signature"])

                messages.success(request, "User created successfully.")
                return redirect(f"/users/{user_obj.pk}/edit/?saved=created")
            except Exception as exc:
                form.add_error(None, f"Could not create user: {exc}")

        errors = _form_error_messages(form, profile_form)
        return render(
            request,
            "accounts/user_form.html",
            _user_form_context(
                form=form,
                profile_form=profile_form,
                profile=empty_profile,
                page_title="Create User",
                submit_label="Save User",
                errors=errors,
            ),
        )

    form = UserCreateForm()
    profile_form = UserProfileForm()

    return render(
        request,
        "accounts/user_form.html",
        _user_form_context(
            form=form,
            profile_form=profile_form,
            profile=empty_profile,
            page_title="Create User",
            submit_label="Save User",
        ),
    )


@login_required
@permission_required("auth.change_user", raise_exception=True)
def user_edit(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    profile, _ = UserProfile.objects.get_or_create(user=user_obj)

    if request.method == "POST":
        form = UserEditForm(request.POST, instance=user_obj)
        profile_form = UserProfileForm(
            request.POST,
            request.FILES,
            instance=profile,
        )

        decoded_signature = None
        signature_error = None

        if profile_form.is_valid():
            try:
                decoded_signature = _decode_signature_data_url(
                    profile_form.cleaned_data.get("signature_data") or ""
                )
            except ValidationError as exc:
                signature_error = str(exc.message)
                profile_form.add_error("signature_data", signature_error)

        if form.is_valid() and profile_form.is_valid() and not signature_error:
            try:
                with transaction.atomic():
                    updated_user = form.save()
                    profile, _ = UserProfile.objects.get_or_create(user=updated_user)

                    remove_signature = request.POST.get("remove_signature") == "1"

                    # New image wins over remove flag.
                    if decoded_signature:
                        _replace_signature(profile, decoded_signature)
                    elif profile_form.cleaned_data.get("signature"):
                        if profile.signature:
                            try:
                                profile.signature.delete(save=False)
                            except Exception:
                                pass
                        profile.signature = profile_form.cleaned_data["signature"]
                        profile.save(update_fields=["signature"])
                    elif remove_signature:
                        _remove_signature(profile)

                selected_group = form.cleaned_data.get("role")
                role_name = selected_group.name if selected_group else "No role"
                messages.success(
                    request,
                    f"User updated successfully. Active role: {role_name}.",
                )
                return redirect(f"/users/{user_obj.pk}/edit/?saved=updated")
            except Exception as exc:
                form.add_error(None, f"Could not update user: {exc}")

        errors = _form_error_messages(form, profile_form)
        return render(
            request,
            "accounts/user_form.html",
            _user_form_context(
                form=form,
                profile_form=profile_form,
                profile=profile,
                user_obj=user_obj,
                page_title="Edit User",
                submit_label="Update User",
                errors=errors,
            ),
        )

    form = UserEditForm(instance=user_obj)
    profile_form = UserProfileForm(instance=profile)

    saved = (request.GET.get("saved") or "").strip().lower()
    page_alert = ""
    if saved == "created":
        page_alert = "User created successfully."
    elif saved == "updated":
        page_alert = "User updated successfully."

    return render(
        request,
        "accounts/user_form.html",
        _user_form_context(
            form=form,
            profile_form=profile_form,
            profile=profile,
            user_obj=user_obj,
            page_title="Edit User",
            submit_label="Update User",
            page_alert=page_alert,
        ),
    )


@login_required
@permission_required("auth.view_group", raise_exception=True)
def role_list(request):
    roles = Group.objects.prefetch_related("permissions").order_by("name")
    return render(request, "accounts/role_list.html", {"roles": roles})


@login_required
@permission_required("auth.view_permission", raise_exception=True)
def permission_list(request):
    permissions = (
        Permission.objects.select_related("content_type")
        .order_by("content_type__app_label", "content_type__model", "name")
    )
    return render(
        request,
        "accounts/permission_list.html",
        {"permissions": permissions},
    )


@login_required
@permission_required("auth.add_group", raise_exception=True)
def role_create(request):
    if request.method == "POST":
        form = RoleForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Role created successfully.")
            return redirect("role_list")
    else:
        form = RoleForm()

    grouped_permissions = defaultdict(list)
    for perm in Permission.objects.select_related("content_type").order_by(
        "content_type__app_label",
        "codename",
    ):
        grouped_permissions[perm.content_type.app_label.upper()].append(perm)

    return render(
        request,
        "accounts/role_form.html",
        {
            "form": form,
            "grouped_permissions": dict(grouped_permissions),
            "page_title": "Create Role",
            "submit_label": "Save Role",
        },
    )


@login_required
@permission_required("auth.change_group", raise_exception=True)
def role_edit(request, pk):
    role = get_object_or_404(Group, pk=pk)

    if request.method == "POST":
        form = RoleForm(request.POST, instance=role)
        if form.is_valid():
            form.save()
            messages.success(request, "Role updated successfully.")
            return redirect("role_list")
    else:
        form = RoleForm(instance=role)

    grouped_permissions = defaultdict(list)
    for perm in Permission.objects.select_related("content_type").order_by(
        "content_type__app_label",
        "codename",
    ):
        grouped_permissions[perm.content_type.app_label.upper()].append(perm)

    return render(
        request,
        "accounts/role_form.html",
        {
            "form": form,
            "role": role,
            "grouped_permissions": dict(grouped_permissions),
            "page_title": "Edit Role",
            "submit_label": "Update Role",
        },
    )