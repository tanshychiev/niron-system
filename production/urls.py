from django.urls import path

from . import views


urlpatterns = [
    path("", views.dashboard, name="production_dashboard"),
    path("materials/", views.material_stock, name="production_material_stock"),
    path("materials/receive/", views.fabric_receipt_create, name="production_fabric_receive"),
    path("materials/receipts/<int:pk>/edit/", views.fabric_receipt_edit, name="production_fabric_receipt_edit"),

    path("projects/", views.project_list, name="production_project_list"),
    path("projects/new/", views.project_create, name="production_project_create"),
    path("projects/<int:pk>/", views.project_detail, name="production_project_detail"),
    path("projects/<int:pk>/rolls/add/", views.project_add_roll, name="production_project_add_roll"),
    path("projects/<int:pk>/rolls/<int:usage_id>/remove/", views.project_remove_roll, name="production_project_remove_roll"),
    path("projects/<int:pk>/plan-sizes/save/", views.project_save_plan_sizes, name="production_project_save_plan_sizes"),
    path("projects/<int:pk>/cut-sizes/save/", views.project_save_cut_sizes, name="production_project_save_cut_sizes"),
    path("projects/<int:pk>/confirm-cutting/", views.project_confirm_cutting, name="production_project_confirm_cutting"),
    path("projects/<int:project_id>/staff-payable/add/", views.staff_payable_create, name="production_staff_payable_create"),

    path("partners/", views.partner_list, name="production_partner_list"),
    path("partners/new/", views.partner_create, name="production_partner_create"),

    path("projects/<int:project_id>/sewing/new/", views.sewing_job_create, name="production_sewing_job_create"),
    path("sewing/<int:pk>/edit/", views.sewing_job_edit, name="production_sewing_job_edit"),
    path("sewing/<int:job_id>/returns/new/", views.sewing_return_create, name="production_return_create"),
    path("returns/", views.sewing_return_list, name="production_return_list"),
    path("returns/<int:pk>/", views.sewing_return_detail, name="production_return_detail"),
    path("returns/<int:pk>/confirm/", views.sewing_return_confirm, name="production_return_confirm"),

    path("payments/<str:payable_type>/", views.payment_list, name="production_payment_list"),
]
