from django.urls import path, re_path
from . import views

urlpatterns = [
    path('machines/', views.machine_daily_list_view, name='machine_daily_list'),
    path('pending-starts/', views.pending_starts_report_view, name='pending_starts_report'),
    path('consultant-stats/', views.consultant_monthly_stats_view, name='consultant_stats'),
    path('unit-stats/', views.unit_monthly_stats_view, name='unit_stats'),
    path('simulations/', views.simulation_report_view, name='simulation_report'),
    path('emergency-slots/', views.emergency_slots_report_view, name='emergency_slots_report'),
    path('manual-grid/', views.manual_schedule_grid_view, name='manual_schedule_grid'),
    re_path(r'^manual-grid/&(.*)$', views.manual_grid_ampersand_redirect, name='manual_grid_ampersand_redirect'),
    path('manual-grid/save-row/', views.manual_schedule_save_row_api, name='manual_schedule_save_row'),
    path('print-schedule/', views.print_treatment_schedule_view, name='print_treatment_schedule'),
    path('print-schedule/pdf/', views.print_treatment_schedule_pdf_view, name='print_treatment_schedule_pdf'),
    path('print-emergency-schedule/', views.print_emergency_schedule_view, name='print_emergency_schedule'),
    path('print-emergency-schedule/pdf/', views.print_emergency_schedule_pdf_view, name='print_emergency_schedule_pdf'),
    path('print-template-settings/', views.print_template_settings_view, name='print_template_settings'),
    path('audit-logs/', views.audit_log_report_view, name='audit_log_report'),
]
