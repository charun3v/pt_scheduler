from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('apps.core.urls')),
    path('patients/', include('apps.patients.urls')),
    path('scheduler/', include('apps.scheduler.urls')),
    path('reports/', include('apps.reports.urls')),
]
