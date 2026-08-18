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
# STAFF SALARY / PAYROLL
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
    """
    Salary effective on the first day of the selected month.
    If a salary upgrade should affect August, set effective date to 01 Aug.
    """
    month_start = _month_start(year, month)
    return (
        profile.salary_history
        .filter(effective_date__lte=month_start)
        .order_by("-effective_date", "-id")
        .first()
    )


def _get_or_create_month_payroll(profile, year, month, user):
    payroll = StaffPayroll.objects.filter(
        staff=profile,
        year=year,
        month=month,
    ).first()

    if payroll:
        return payroll

    salary_record = _salary_record_for_month(profile, year, month)

    if not salary_record:
        raise ValidationError(
            f"Set a base salary for {profile.staff_name} before making payroll."
        )

    payroll = StaffPayroll.objects.create(
        staff=profile,
        year=year,
        month=month,
        base_salary=salary_record.salary,
        created_by=user,
    )
    return payroll


def _create_finance_expense(*, user, amount, note):
    """
    Reuse the existing Finance Expense model used by production payments.
    """
    from finance.models import Expense

    return Expense.objects.create(
        created_at=timezone.now(),
        created_by=user,
        expense_type=Expense.TYPE_OPERATING,
        amount=amount,
        category=Expense.OPERATING_OTHER,
        note=note,
    )


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
def staff_payroll(request):
    year, month = _selected_period(request)

    profiles = (
        UserProfile.objects
        .filter(is_staff_employee=True)
        .select_related("user")
        .prefetch_related("salary_history", "payrolls__payments")
        .order_by("user__first_name", "user__last_name", "user__username")
    )

    rows = []
    selected_month_start = _month_start(year, month)
    _, days_in_month = calendar.monthrange(year, month)
    selected_month_end = date(year, month, days_in_month)

    for profile in profiles:
        # Do not show someone before their join month.
        if profile.join_date and profile.join_date > selected_month_end:
            continue

        # Do not show someone after their left month.
        if profile.left_date and profile.left_date < selected_month_start:
            continue

        payroll = (
            StaffPayroll.objects
            .filter(staff=profile, year=year, month=month)
            .prefetch_related("payments")
            .first()
        )

        salary_record = _salary_record_for_month(profile, year, month)
        display_base_salary = (
            Decimal(payroll.base_salary or 0)
            if payroll
            else Decimal(salary_record.salary or 0)
            if salary_record
            else ZERO
        )

        salary_history = list(
            profile.salary_history.all()[:8]
        )

        rows.append(
            {
                "profile": profile,
                "payroll": payroll,
                "salary_record": salary_record,
                "salary_history": salary_history,
                "base_salary": display_base_salary,
                "first_payment": Decimal(payroll.first_payment or 0) if payroll else ZERO,
                "remaining_salary": payroll.remaining_salary if payroll else display_base_salary,
                "commission": Decimal(payroll.commission or 0) if payroll else ZERO,
                "bonus": Decimal(payroll.bonus or 0) if payroll else ZERO,
                "deduction": Decimal(payroll.deduction or 0) if payroll else ZERO,
                "final_due": payroll.final_due if payroll else display_base_salary,
                "status": payroll.status if payroll else StaffPayroll.STATUS_OPEN,
            }
        )

    history = (
        StaffPayrollPayment.objects
        .filter(payroll__year=year, payroll__month=month)
        .select_related("payroll__staff__user", "created_by")
        .order_by("-payment_date", "-id")
    )

    total_base = sum((row["base_salary"] for row in rows), ZERO)
    total_first = sum((row["first_payment"] for row in rows), ZERO)
    total_remaining = sum((row["remaining_salary"] for row in rows), ZERO)
    total_commission = sum((row["commission"] for row in rows), ZERO)

    previous_month = 12 if month == 1 else month - 1
    previous_year = year - 1 if month == 1 else year
    next_month = 1 if month == 12 else month + 1
    next_year = year + 1 if month == 12 else year

    return render(
        request,
        "accounts/staff_payroll.html",
        {
            "rows": rows,
            "history": history,
            "year": year,
            "month": month,
            "month_name": calendar.month_name[month],
            "month_choices": StaffPayroll.MONTH_CHOICES,
            "today": timezone.localdate(),
            "total_base": total_base,
            "total_first": total_first,
            "total_remaining": total_remaining,
            "total_commission": total_commission,
            "previous_year": previous_year,
            "previous_month": previous_month,
            "next_year": next_year,
            "next_month": next_month,
        },
    )


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
@transaction.atomic
def staff_salary_add(request, staff_id):
    profile = get_object_or_404(
        UserProfile.objects.select_related("user"),
        pk=staff_id,
        is_staff_employee=True,
    )

    year, month = _selected_period(request)

    if request.method != "POST":
        return redirect(
            f"/production/payments/staff/?year={year}&month={month}"
        )

    form = StaffSalaryHistoryForm(request.POST)

    if form.is_valid():
        salary = form.save(commit=False)
        salary.staff = profile
        salary.created_by = request.user
        salary.save()

        messages.success(
            request,
            f"Salary saved for {profile.staff_name}: "
            f"${salary.salary:.2f} effective {salary.effective_date:%d %b %Y}.",
        )
    else:
        for error in _form_error_messages(form):
            messages.error(request, error)

    return redirect(
        f"/production/payments/staff/?year={year}&month={month}"
    )


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
@transaction.atomic
def staff_first_payment(request, staff_id):
    profile = get_object_or_404(
        UserProfile.objects.select_related("user"),
        pk=staff_id,
        is_staff_employee=True,
    )

    year, month = _selected_period(request)

    if request.method != "POST":
        return redirect(
            f"/production/payments/staff/?year={year}&month={month}"
        )

    form = StaffFirstPaymentForm(request.POST)

    if form.is_valid():
        try:
            payroll = _get_or_create_month_payroll(
                profile,
                year,
                month,
                request.user,
            )

            if payroll.status == StaffPayroll.STATUS_COMPLETED:
                raise ValidationError("This payroll is already completed.")

            if StaffPayrollPayment.objects.filter(
                payroll=payroll,
                payment_type=StaffPayrollPayment.TYPE_FIRST,
            ).exists():
                raise ValidationError("First payment was already recorded.")

            amount = Decimal(form.cleaned_data["amount"] or 0)

            if amount > Decimal(payroll.base_salary or 0):
                raise ValidationError(
                    "First payment cannot be greater than the base salary."
                )

            payment = StaffPayrollPayment.objects.create(
                payroll=payroll,
                payment_type=StaffPayrollPayment.TYPE_FIRST,
                payment_date=form.cleaned_data["payment_date"],
                amount=amount,
                payment_method=form.cleaned_data["payment_method"],
                reference=form.cleaned_data.get("reference") or "",
                note=form.cleaned_data.get("note") or "",
                created_by=request.user,
            )

            payroll.first_payment = amount
            payroll.first_payment_date = payment.payment_date
            payroll.status = StaffPayroll.STATUS_FIRST_PAID
            payroll.save(
                update_fields=[
                    "first_payment",
                    "first_payment_date",
                    "status",
                    "updated_at",
                ]
            )

            expense = _create_finance_expense(
                user=request.user,
                amount=amount,
                note=(
                    f"Staff salary first payment to {profile.staff_name}. "
                    f"{payroll.get_month_display()} {payroll.year}. "
                    f"Base salary ${payroll.base_salary:.2f}. "
                    f"First payment ${amount:.2f}. "
                    f"Remaining ${payroll.remaining_salary:.2f}. "
                    f"Method: {payment.get_payment_method_display()}. "
                    f"Reference: {payment.reference or '-'}."
                ),
            )

            payment.finance_expense_id = expense.id
            payment.save(update_fields=["finance_expense_id"])

            messages.success(
                request,
                f"First payment recorded for {profile.staff_name}: "
                f"${amount:.2f}. Remaining base salary: "
                f"${payroll.remaining_salary:.2f}.",
            )

        except ValidationError as exc:
            for error in exc.messages:
                messages.error(request, error)

    else:
        for error in _form_error_messages(form):
            messages.error(request, error)

    return redirect(
        f"/production/payments/staff/?year={year}&month={month}"
    )


@login_required
@permission_required("production.manage_production_payments", raise_exception=True)
@transaction.atomic
def staff_final_payment(request, staff_id):
    profile = get_object_or_404(
        UserProfile.objects.select_related("user"),
        pk=staff_id,
        is_staff_employee=True,
    )

    year, month = _selected_period(request)

    if request.method != "POST":
        return redirect(
            f"/production/payments/staff/?year={year}&month={month}"
        )

    form = StaffFinalPaymentForm(request.POST)

    if form.is_valid():
        try:
            payroll = _get_or_create_month_payroll(
                profile,
                year,
                month,
                request.user,
            )

            if payroll.status == StaffPayroll.STATUS_COMPLETED:
                raise ValidationError("This payroll is already completed.")

            if not StaffPayrollPayment.objects.filter(
                payroll=payroll,
                payment_type=StaffPayrollPayment.TYPE_FIRST,
            ).exists():
                raise ValidationError(
                    "Record the first payment before the final payment."
                )

            if StaffPayrollPayment.objects.filter(
                payroll=payroll,
                payment_type=StaffPayrollPayment.TYPE_FINAL,
            ).exists():
                raise ValidationError("Final payment was already recorded.")

            payroll.commission = form.cleaned_data["commission"]
            payroll.bonus = form.cleaned_data["bonus"]
            payroll.deduction = form.cleaned_data["deduction"]

            final_due = payroll.final_due

            if final_due <= 0:
                raise ValidationError(
                    "Final payment amount must be greater than zero."
                )

            payment = StaffPayrollPayment.objects.create(
                payroll=payroll,
                payment_type=StaffPayrollPayment.TYPE_FINAL,
                payment_date=form.cleaned_data["payment_date"],
                amount=final_due,
                payment_method=form.cleaned_data["payment_method"],
                reference=form.cleaned_data.get("reference") or "",
                note=form.cleaned_data.get("note") or "",
                created_by=request.user,
            )

            payroll.final_payment = final_due
            payroll.final_payment_date = payment.payment_date
            payroll.status = StaffPayroll.STATUS_COMPLETED
            payroll.save(
                update_fields=[
                    "commission",
                    "bonus",
                    "deduction",
                    "final_payment",
                    "final_payment_date",
                    "status",
                    "updated_at",
                ]
            )

            expense = _create_finance_expense(
                user=request.user,
                amount=final_due,
                note=(
                    f"Staff salary final payment to {profile.staff_name}. "
                    f"{payroll.get_month_display()} {payroll.year}. "
                    f"Remaining base salary ${payroll.remaining_salary:.2f}. "
                    f"Manual commission ${payroll.commission:.2f}. "
                    f"Bonus ${payroll.bonus:.2f}. "
                    f"Deduction ${payroll.deduction:.2f}. "
                    f"Final payment ${final_due:.2f}. "
                    f"Method: {payment.get_payment_method_display()}. "
                    f"Reference: {payment.reference or '-'}."
                ),
            )

            payment.finance_expense_id = expense.id
            payment.save(update_fields=["finance_expense_id"])

            messages.success(
                request,
                f"Final salary payment completed for {profile.staff_name}: "
                f"${final_due:.2f}.",
            )

        except ValidationError as exc:
            for error in exc.messages:
                messages.error(request, error)

    else:
        for error in _form_error_messages(form):
            messages.error(request, error)

    return redirect(
        f"/production/payments/staff/?year={year}&month={month}"
    )
