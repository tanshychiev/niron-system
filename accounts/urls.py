from django.urls import path

from . import views


urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),

    path("users/", views.user_list, name="user_list"),
    path("users/new/", views.user_create, name="user_create"),
    path("users/<int:pk>/edit/", views.user_edit, name="user_edit"),

    path("roles/", views.role_list, name="role_list"),
    path("roles/new/", views.role_create, name="role_create"),
    path("roles/<int:pk>/edit/", views.role_edit, name="role_edit"),

    path("permissions/", views.permission_list, name="permission_list"),

    # Canonical staff payroll routes
    path(
        "staff-payroll/",
        views.staff_payroll,
        name="staff_payroll",
    ),
    path(
        "staff-payroll/<int:staff_id>/salary/",
        views.staff_salary_add,
        name="staff_salary_add",
    ),
    path(
        "staff-payroll/<int:staff_id>/first-payment/",
        views.staff_first_payment,
        name="staff_first_payment",
    ),
    path(
        "staff-payroll/<int:staff_id>/final-payment/",
        views.staff_final_payment,
        name="staff_final_payment",
    ),
]
