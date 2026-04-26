from collections import defaultdict

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import Group, Permission, User
from django.shortcuts import get_object_or_404, redirect, render

from .forms import LoginForm, RoleForm, UserCreateForm, UserEditForm, UserProfileForm
from .models import UserProfile


def login_view(request):
    if request.user.is_authenticated:
        return redirect("inventory_list")

    form = LoginForm(request, data=request.POST or None)

    if request.method == "POST":
        if form.is_valid():
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
    if request.method == "POST":
        form = UserCreateForm(request.POST)
        profile_form = UserProfileForm(request.POST, request.FILES)

        if form.is_valid() and profile_form.is_valid():
            user_obj = form.save()
            profile, _ = UserProfile.objects.get_or_create(user=user_obj)

            profile_form = UserProfileForm(
                request.POST,
                request.FILES,
                instance=profile,
            )
            profile_form.save()

            messages.success(request, "User created successfully.")
            return redirect("user_list")
    else:
        form = UserCreateForm()
        profile_form = UserProfileForm()

    return render(
        request,
        "accounts/user_form.html",
        {
            "form": form,
            "profile_form": profile_form,
            "page_title": "Create User",
            "submit_label": "Save User",
        },
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

        if form.is_valid() and profile_form.is_valid():
            form.save()
            profile_form.save()
            messages.success(request, "User updated successfully.")
            return redirect("user_list")
    else:
        form = UserEditForm(instance=user_obj)
        profile_form = UserProfileForm(instance=profile)

    return render(
        request,
        "accounts/user_form.html",
        {
            "form": form,
            "profile_form": profile_form,
            "user_obj": user_obj,
            "page_title": "Edit User",
            "submit_label": "Update User",
        },
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
        {
            "permissions": permissions,
        },
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