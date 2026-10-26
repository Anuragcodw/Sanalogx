from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

urlpatterns = [
    path('admin/', admin.site.urls),

    # Accounts & Authentication Architecture
    path('', include('accounts.urls')),
    path('medicines/', include('medicines.urls')),
    path('inventory/', include('inventory.urls')),
    path('', include('dispensing.urls')),
    path('', include('suppliers.urls')),
    path('', include('purchases.urls')),
    path('', include('ai_engine.urls')),
    path('', include('notifications.urls')),
    path('', include('reports.urls')),
    path('', include('sales.urls')),
    path('integrations/', include('integrations.urls')),
    path('api/v1/fhir/', include('integrations.api_urls')),

    # API Schema & Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

handler404 = 'accounts.views.not_found_404_view'
handler403 = 'accounts.views.unauthorized_403_view'

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
