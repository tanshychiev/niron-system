from django.urls import path
from .views import (
    login_view,
    logout_view,
    user_list,
    user_create,
    user_edit,
    role_list,
    role_create,
    role_edit,
    permission_list,
)

urlpatterns = [
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),

    path("users/", user_list, name="user_list"),
    path("users/new/", user_create, name="user_create"),
    path("users/<int:pk>/edit/", user_edit, name="user_edit"),

    path("users/roles/", role_list, name="role_list"),
    path("users/roles/create/", role_create, name="role_create"),
    path("users/roles/<int:pk>/edit/", role_edit, name="role_edit"),

    path("users/permissions/", permission_list, name="permission_list"),
]