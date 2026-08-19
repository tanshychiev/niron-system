from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path, re_path


def niron_home(request):
    return redirect("inventory_list")


def unknown_page(request, unmatched_path=None):
    return redirect("inventory_list")


urlpatterns = [
    path("admin/", admin.site.urls),

    # Niron main page
    path("", niron_home, name="niron_home"),

    # Accounts
    path("", include("accounts.urls")),

    # Modules
    path("orders/", include("orders.urls")),
    path("inventory/", include("inventory.urls")),
    path("production/", include("production.urls")),
    path("finance/", include("finance.urls")),
    path("customers/", include("customers.urls")),
]


# Serve static + media files while DEBUG=True
if settings.DEBUG:
    urlpatterns += static(
        settings.STATIC_URL,
        document_root=settings.STATIC_ROOT,
    )

    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT,
    )


# Keep this LAST.
# Any unknown URL goes back to Inventory.
urlpatterns += [
    re_path(
        r"^(?P<unmatched_path>.*)$",
        unknown_page,
        name="unknown_page",
    ),
]