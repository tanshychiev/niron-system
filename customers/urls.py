from django.urls import path
from . import views

urlpatterns = [
    path("", views.customer_list, name="customer_list"),
    path("search/", views.customer_search, name="customer_search"),
    path("<int:pk>/edit/", views.customer_edit, name="customer_edit"),
    path("<int:pk>/", views.customer_detail, name="customer_detail"),
]