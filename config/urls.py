from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path("admin/", admin.site.urls),

    path("", include("accounts.urls")),
    path("orders/", include("orders.urls")),
    path("inventory/", include("inventory.urls")),
    path("admin/", admin.site.urls),
    path("", include("accounts.urls")),
    path("", include("inventory.urls")),
    path("", include("orders.urls")),
    path("", include("finance.urls")),
    path("customers/", include("customers.urls")),


]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)