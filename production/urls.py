from django.urls import path

from accounts import views as account_views
from . import views


urlpatterns = [
    path("", views.dashboard, name="production_dashboard"),

    # Materials
    path(
        "materials/",
        views.material_stock,
        name="production_material_stock",
    ),
    path(
        "materials/receive/",
        views.fabric_receipt_create,
        name="production_fabric_receive",
    ),
    path(
        "materials/receipts/<int:pk>/edit/",
        views.fabric_receipt_edit,
        name="production_fabric_receipt_edit",
    ),

    # Fabric types
    path(
        "fabric-types/",
        views.fabric_type_list,
        name="production_fabric_type_list",
    ),
    path(
        "fabric-types/new/",
        views.fabric_type_create,
        name="production_fabric_type_create",
    ),
    path(
        "fabric-types/<int:pk>/edit/",
        views.fabric_type_edit,
        name="production_fabric_type_edit",
    ),

    # Production projects
    path(
        "projects/",
        views.project_list,
        name="production_project_list",
    ),
    path(
        "projects/new/",
        views.project_create,
        name="production_project_create",
    ),
    path(
        "projects/<int:pk>/",
        views.project_detail,
        name="production_project_detail",
    ),
    path(
        "projects/<int:pk>/edit/",
        views.project_edit,
        name="production_project_edit",
    ),

    # Fabric roll selection
    path(
        "projects/<int:pk>/rolls/add/",
        views.project_add_roll,
        name="production_project_add_roll",
    ),
    path(
        "projects/<int:pk>/rolls/<int:usage_id>/remove/",
        views.project_remove_roll,
        name="production_project_remove_roll",
    ),

    # Plan and cutting
    path(
        "projects/<int:pk>/plan-sizes/save/",
        views.project_save_plan_sizes,
        name="production_project_save_plan_sizes",
    ),
    path(
        "projects/<int:pk>/cut-sizes/save/",
        views.project_save_cut_sizes,
        name="production_project_save_cut_sizes",
    ),
    path(
        "projects/<int:pk>/update-fabric-remaining/",
        views.project_update_fabric_remaining,
        name="production_project_update_fabric_remaining",
    ),
    path(
        "projects/<int:pk>/confirm-cutting/",
        views.project_confirm_cutting,
        name="production_project_confirm_cutting",
    ),

    # Sewing workflow
    path(
        "projects/<int:pk>/send-sewing/",
        views.project_send_sewing_inline,
        name="production_project_send_sewing_inline",
    ),
    path(
        "projects/<int:pk>/sewing/receive-selected/",
        views.project_receive_selected_jobs,
        name="production_project_receive_selected_jobs",
    ),
    path(
        "projects/<int:pk>/sewing/<int:job_id>/receive/",
        views.project_receive_sewing_inline,
        name="production_project_receive_sewing_inline",
    ),

    # Safe undo
    path(
        "projects/<int:pk>/undo/reopen-cutting/",
        views.project_reopen_cutting,
        name="production_project_reopen_cutting",
    ),
    path(
        "projects/<int:pk>/undo/latest-send/",
        views.project_undo_latest_sewing_job,
        name="production_project_undo_latest_sewing_job",
    ),
    path(
        "projects/<int:pk>/undo/latest-receive/",
        views.project_undo_latest_sewing_receive,
        name="production_project_undo_latest_sewing_receive",
    ),

    # Staff payable
    path(
        "projects/<int:project_id>/staff-payable/add/",
        views.staff_payable_create,
        name="production_staff_payable_create",
    ),

    # Partners and suppliers
    path(
        "partners/",
        views.partner_list,
        name="production_partner_list",
    ),
    path(
        "partners/new/",
        views.partner_create,
        name="production_partner_create",
    ),
    path(
        "partners/<int:pk>/",
        views.partner_detail,
        name="production_partner_detail",
    ),
    path(
        "suppliers/",
        views.supplier_list,
        name="production_supplier_list",
    ),
    path(
        "suppliers/new/",
        views.supplier_create,
        name="production_supplier_create",
    ),
    path(
        "suppliers/<int:pk>/",
        views.supplier_detail,
        name="production_supplier_detail",
    ),

    # Production expenses
    path(
        "expenses/",
        views.production_expense_list,
        name="production_expense_list",
    ),
    path(
        "expenses/new/",
        views.production_expense_create,
        name="production_expense_create",
    ),

    # Sewing jobs and returns
    path(
        "projects/<int:project_id>/sewing/new/",
        views.sewing_job_create,
        name="production_sewing_job_create",
    ),
    path(
        "sewing/<int:pk>/edit/",
        views.sewing_job_edit,
        name="production_sewing_job_edit",
    ),
    path(
        "sewing/<int:job_id>/returns/new/",
        views.sewing_return_create,
        name="production_return_create",
    ),
    path(
        "returns/",
        views.sewing_return_list,
        name="production_return_list",
    ),
    path(
        "returns/bulk-pay/",
        views.sewing_return_bulk_pay,
        name="production_return_bulk_pay",
    ),
    path(
        "returns/<int:pk>/",
        views.sewing_return_detail,
        name="production_return_detail",
    ),
    path(
        "returns/<int:pk>/confirm/",
        views.sewing_return_confirm,
        name="production_return_confirm",
    ),

    # Staff monthly payroll
    path(
        "payments/staff/",
        account_views.staff_payroll,
        name="production_staff_payroll",
    ),

    # Existing sewing / project payments
    path(
        "payments/<str:payable_type>/",
        views.payment_list,
        name="production_payment_list",
    ),
]