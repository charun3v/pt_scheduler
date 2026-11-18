from django.urls import path
from . import views
from apps.reports import views as report_views

urlpatterns = [
    path('patient/<int:patient_id>/', views.schedule_patient_view, name='schedule_patient'),
    path('patient/<int:patient_id>/brachytherapy/', views.brachy_schedule_patient_view, name='brachy_schedule_patient'),
    path('brachytherapy/calendar/', views.brachy_calendar_view, name='brachy_calendar'),
    path('brachytherapy/daily-list/', views.brachy_daily_patients_view, name='brachy_daily_patients'),
    path('brachytherapy/api/daily-list/', views.brachy_daily_patients_api, name='brachy_daily_patients_api'),
    path('brachytherapy/api/appointment/<int:appointment_id>/edit/', views.brachy_edit_appointment_api, name='brachy_edit_appointment_api'),
    path('brachytherapy/api/month/', views.brachy_calendar_month_api, name='brachy_calendar_month_api'),
    path('schedule/<int:schedule_id>/delete/', views.schedule_delete_view, name='schedule_delete'),
    path('course/<int:schedule_id>/tag/', views.tag_treatment_course_view, name='tag_treatment_course'),
    path('course/<int:schedule_id>/actual-status/', views.update_actual_treatment_status_view, name='update_actual_treatment_status'),
    path('course/<int:schedule_id>/emergency-slot/', views.emergency_slot_view, name='emergency_slot_view'),
    path('emergency-slot/<int:schedule_id>/', views.emergency_slot_view, name='emergency_slot_direct'),
    path('course/<int:schedule_id>/revert-emergency/', views.revert_emergency_slot_view, name='revert_emergency_slot'),
    path('unscheduled/', views.unscheduled_patients_view, name='unscheduled_patients'),
    path('mappings/', views.machine_mappings_view, name='machine_mappings'),
    path('mappings/<int:mapping_id>/edit/', views.edit_machine_mapping_view, name='edit_machine_mapping'),
    path('holidays/', views.holiday_manager_view, name='holiday_manager'),
    path('manual-grid/', report_views.manual_schedule_grid_view, name='scheduler_manual_grid'),
    
    # API endpoints
    path('api/machine-options/<int:machine_id>/', views.get_machine_options_api, name='api_machine_options'),
    path('api/nearest-free-slot/', views.get_nearest_free_slot_api, name='api_nearest_free_slot'),
    path('api/mapped-machine-slots/', views.get_mapped_machines_slots_api, name='api_mapped_machine_slots'),
]
