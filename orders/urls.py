from django.urls import path
from . import views

urlpatterns = [
    path("", views.order_list, name="order_list"),
    path("new/", views.order_create, name="order_create"),
    path("<int:pk>/", views.order_detail, name="order_detail"),
    path("<int:pk>/edit/", views.order_edit, name="order_edit"),
    path("export-excel/", views.order_list_export_excel, name="order_list_export_excel"),
    path("<int:pk>/invoice/", views.order_invoice, name="order_invoice"),
    path("<int:pk>/invoice/pdf/", views.order_invoice_pdf, name="order_invoice_pdf"),

    path("production/", views.production_list, name="production_list"),
    path("production/<int:pk>/", views.production_detail, name="production_detail"),
    path("production/<int:pk>/update/", views.production_update, name="production_update"),
]