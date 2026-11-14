from django.urls import path
from . import views

urlpatterns = [
    path('machines/', views.machine_daily_list_view, name='machine_daily_list'),
    path('pending-starts/', views.pending_starts_report_view, name='pending_starts_report'),
    path('consultant-stats/', views.consultant_monthly_stats_view, name='consultant_stats'),
]
