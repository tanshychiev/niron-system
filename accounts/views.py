import base64
import binascii
import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal
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
    StaffFinalPaymentForm,
    StaffFirstPaymentForm,
    StaffSalaryHistoryForm,
    UserCreateForm,
    UserEditForm,
    UserProfileForm,
)
from .models import (
    StaffPayroll,
    StaffPayrollPayment,
    StaffSalaryHistory,
    UserProfile,
)


ZERO = Decimal("0.00")

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


def _save_profile_staff_fields(profile, profile_form):
    """
    Save staff fields from UserProfileForm without touching the saved signature
    unless the signature-specific code below asks us to.
    """
    profile.is_staff_employee = bool(
        profile_form.cleaned_data.get("is_staff_employee")
    )
    profile.join_date = profile_form.cleaned_data.get("join_date")
    profile.left_date = profile_form.cleaned_data.get("left_date")
    profile.staff_note = profile_form.cleaned_data.get("staff_note") or ""
    profile.save(
        update_fields=[
            "is_staff_employee",
            "join_date",
            "left_date",
            "staff_note",
        ]
    )


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


@login_required
def logout_view(request):
    logout(request)
    messages.success(request, "Logged out successfully.")
    return redirect("login")


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

                    _save_profile_staff_fields(profile, profile_form)

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

                    _save_profile_staff_fields(profile, profile_form)

                    remove_signature = request.POST.get("remove_signature") == "1"

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


# ============================================================
# SIMPLE STAFF SALARY / MONTHLY EXPENSE
# ============================================================

def _selected_period(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year") or request.POST.get("year") or today.year)
    except (TypeError, ValueError):
        year = today.year
    try:
        month = int(request.GET.get("month") or request.POST.get("month") or today.month)
    except (TypeError, ValueError):
        month = today.month
    if month < 1 or month > 12:
        month = today.month
    if year < 2000 or year > 2100:
        year = today.year
    return year, month


def _month_start(year, month):
    return date(year, month, 1)


def _salary_record_for_month(profile, year, month):
    month_start = _month_start(year, month)
    return (
        profile.salary_history
        .filter(effective_date__lte=month_start)
        .order_by("-effective_date", "-id")
        .first()
    )


def _staff_is_active_for_month(profile, year, month):
    month_start = _month_start(year, month)
    _, days = calendar.monthrange(year, month)
    month_end = date(year, month, days)
    if profile.join_date and profile.join_date > month_end:
        return False
    if profile.left_date and profile.left_date < month_start:
        return False
    return profile.is_staff_employee


def _sync_payroll_expense(payroll, user):
    """One automatic Finance salary expense per staff per month."""
    from finance.models import Expense

    amount = payroll.total_payroll_amount
    expense_date = date(payroll.year, payroll.month, 1)
    note = (
        f"Auto staff salary - {payroll.staff.staff_name} - "
        f"{payroll.get_month_display()} {payroll.year}. "
        f"Base ${payroll.base_salary:.2f}; Commission ${payroll.commission:.2f}; "
        f"Bonus ${payroll.bonus:.2f}; Deduction ${payroll.deduction:.2f}"
    )
    if payroll.deduction and payroll.deduction_reason:
        note += f" ({payroll.deduction_reason})"

    expense = None
    if payroll.finance_expense_id:
        expense = Expense.objects.filter(pk=payroll.finance_expense_id).first()

    if expense is None:
        expense = Expense.objects.create(
            expense_date=expense_date,
            created_by=user,
            expense_type=Expense.TYPE_OPERATING,
            amount=amount,
            category=Expense.OPERATING_SALARY,
            note=note,
        )
        payroll.finance_expense_id = expense.id
        payroll.save(update_fields=["finance_expense_id", "updated_at"])
    else:
        changed = False
        if expense.amount != amount:
            expense.amount = amount
            changed = True
        if expense.expense_date != expense_date:
            expense.expense_date = expense_date
            changed = True
        if expense.category != Expense.OPERATING_SALARY:
            expense.category = Expense.OPERATING_SALARY
            changed = True
        if expense.note != note:
            expense.note = note
            changed = True
        if changed:
            expense.save(update_fields=["amount", "expense_date", "category", "note"])
    return expense


def _ensure_month_payroll(profile, year, month, user):
    payroll = StaffPayroll.objects.filter(staff=profile, year=year, month=month).first()
    if payroll is None:
        salary_record = _salary_record_for_month(profile, year, month)
        base = Decimal(salary_record.salary or 0) if salary_record else ZERO
        payroll = StaffPayroll.objects.create(
            staff=profile,
            year=year,
            month=month,
            base_salary=base,
            created_by=user,
        )
    _sync_payroll_expense(payroll, user)
    return payroll


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
def staff_payroll(request):
    year, month = _selected_period(request)
    profiles = (
        UserProfile.objects
        .filter(is_staff_employee=True)
        .select_related("user")
        .prefetch_related("salary_history")
        .order_by("user__first_name", "user__last_name", "user__username")
    )

    rows = []
    for profile in profiles:
        if not _staff_is_active_for_month(profile, year, month):
            continue
        payroll = _ensure_month_payroll(profile, year, month, request.user)
        rows.append({"profile": profile, "payroll": payroll})

    total_base = sum((Decimal(r["payroll"].base_salary or 0) for r in rows), ZERO)
    total_commission = sum((Decimal(r["payroll"].commission or 0) for r in rows), ZERO)
    total_bonus = sum((Decimal(r["payroll"].bonus or 0) for r in rows), ZERO)
    total_deduction = sum((Decimal(r["payroll"].deduction or 0) for r in rows), ZERO)
    total_expense = sum((r["payroll"].total_payroll_amount for r in rows), ZERO)

    previous_month = 12 if month == 1 else month - 1
    previous_year = year - 1 if month == 1 else year
    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year

    return render(request, "accounts/staff_payroll.html", {
        "rows": rows,
        "year": year,
        "month": month,
        "month_name": calendar.month_name[month],
        "month_choices": StaffPayroll.MONTH_CHOICES,
        "total_base": total_base,
        "total_commission": total_commission,
        "total_bonus": total_bonus,
        "total_deduction": total_deduction,
        "total_expense": total_expense,
        "previous_year": previous_year,
        "previous_month": previous_month,
        "next_year": next_year,
        "next_month": next_month,
    })


def _unique_staff_username(name):
    base = "staff_" + "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    base = (base or "staff")[:120]
    candidate = base
    i = 2
    while User.objects.filter(username=candidate).exists():
        candidate = f"{base[:110]}_{i}"
        i += 1
    return candidate


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
@transaction.atomic
def staff_quick_add(request):
    year, month = _selected_period(request)
    if request.method != "POST":
        return redirect(f"/staff-payroll/?year={year}&month={month}")

    name = (request.POST.get("name") or "").strip()
    position = (request.POST.get("position") or "").strip()
    salary_raw = (request.POST.get("base_salary") or "0").strip()
    join_raw = (request.POST.get("join_date") or "").strip()
    if not name:
        messages.error(request, "Staff name is required.")
        return redirect(f"/staff-payroll/?year={year}&month={month}")
    try:
        salary = Decimal(salary_raw)
        if salary < 0:
            raise ValueError
    except Exception:
        messages.error(request, "Base salary must be 0 or more.")
        return redirect(f"/staff-payroll/?year={year}&month={month}")

    join_date = timezone.localdate()
    if join_raw:
        try:
            join_date = date.fromisoformat(join_raw)
        except ValueError:
            pass

    user = User.objects.create(username=_unique_staff_username(name), first_name=name)
    user.set_unusable_password()
    user.is_active = False
    user.save(update_fields=["password", "is_active"])
    profile, _ = UserProfile.objects.get_or_create(user=user)
    profile.is_staff_employee = True
    profile.join_date = join_date
    profile.left_date = None
    profile.staff_position = position
    profile.save(update_fields=["is_staff_employee", "join_date", "left_date", "staff_position"])
    StaffSalaryHistory.objects.create(
        staff=profile,
        salary=salary,
        effective_date=date(join_date.year, join_date.month, 1),
        note="Starting salary",
        created_by=request.user,
    )
    if _staff_is_active_for_month(profile, year, month):
        _ensure_month_payroll(profile, year, month, request.user)
    messages.success(request, f"Staff {name} created.")
    return redirect(f"/staff-payroll/?year={year}&month={month}")


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
@transaction.atomic
def staff_quick_edit(request, staff_id):
    year, month = _selected_period(request)
    profile = get_object_or_404(UserProfile.objects.select_related("user"), pk=staff_id, is_staff_employee=True)
    if request.method != "POST":
        return redirect(f"/staff-payroll/?year={year}&month={month}")

    name = (request.POST.get("name") or "").strip()
    position = (request.POST.get("position") or "").strip()
    salary_raw = (request.POST.get("default_salary") or "0").strip()
    active = request.POST.get("active") == "1"
    if name:
        profile.user.first_name = name
        profile.user.save(update_fields=["first_name"])
    profile.staff_position = position
    if active:
        profile.left_date = None
    elif not profile.left_date:
        profile.left_date = timezone.localdate()
    profile.save(update_fields=["staff_position", "left_date"])

    try:
        salary = Decimal(salary_raw)
        if salary >= 0:
            effective = _month_start(year, month)
            current = _salary_record_for_month(profile, year, month)
            if current is None or Decimal(current.salary or 0) != salary:
                StaffSalaryHistory.objects.create(
                    staff=profile,
                    salary=salary,
                    effective_date=effective,
                    note="Salary updated",
                    created_by=request.user,
                )
                payroll = StaffPayroll.objects.filter(staff=profile, year=year, month=month).first()
                if payroll:
                    payroll.base_salary = salary
                    payroll.save(update_fields=["base_salary", "updated_at"])
                    _sync_payroll_expense(payroll, request.user)
    except Exception:
        messages.error(request, "Salary was not changed because the amount is invalid.")

    messages.success(request, f"Staff {profile.staff_name} updated.")
    return redirect(f"/staff-payroll/?year={year}&month={month}")


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
@transaction.atomic
def staff_payroll_edit(request, staff_id):
    year, month = _selected_period(request)
    profile = get_object_or_404(UserProfile.objects.select_related("user"), pk=staff_id, is_staff_employee=True)
    if request.method != "POST":
        return redirect(f"/staff-payroll/?year={year}&month={month}")

    payroll = _ensure_month_payroll(profile, year, month, request.user)
    try:
        base = Decimal((request.POST.get("base_salary") or "0").strip())
        commission = Decimal((request.POST.get("commission") or "0").strip())
        bonus = Decimal((request.POST.get("bonus") or "0").strip())
        deduction = Decimal((request.POST.get("deduction") or "0").strip())
        if min(base, commission, bonus, deduction) < 0:
            raise ValueError
    except Exception:
        messages.error(request, "Salary, commission, bonus and deduction must be valid amounts of 0 or more.")
        return redirect(f"/staff-payroll/?year={year}&month={month}")

    reason = (request.POST.get("deduction_reason") or "").strip()
    if deduction > 0 and not reason:
        messages.error(request, "Deduction reason is required when deduction is more than $0.")
        return redirect(f"/staff-payroll/?year={year}&month={month}")

    payroll.base_salary = base
    payroll.commission = commission
    payroll.bonus = bonus
    payroll.deduction = deduction
    payroll.deduction_reason = reason if deduction > 0 else ""
    payroll.note = (request.POST.get("note") or "").strip()
    payroll.save(update_fields=[
        "base_salary", "commission", "bonus", "deduction",
        "deduction_reason", "note", "updated_at"
    ])
    _sync_payroll_expense(payroll, request.user)
    messages.success(request, f"{profile.staff_name} salary expense updated to ${payroll.total_payroll_amount:.2f}.")
    return redirect(f"/staff-payroll/?year={year}&month={month}")


# Backward-compatible old payroll URLs now return to the simple salary screen.
@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
def staff_salary_add(request, staff_id):
    year, month = _selected_period(request)
    return redirect(f"/staff-payroll/?year={year}&month={month}")


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
def staff_first_payment(request, staff_id):
    year, month = _selected_period(request)
    return redirect(f"/staff-payroll/?year={year}&month={month}")


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
def staff_final_payment(request, staff_id):
    year, month = _selected_period(request)
    return redirect(f"/staff-payroll/?year={year}&month={month}")
