from django.urls import path
from . import views
from apps.reports import views as report_views

urlpatterns = [
    path('patient/<int:patient_id>/', views.schedule_patient_view, name='schedule_patient'),
    path('schedule/<int:schedule_id>/delete/', views.schedule_delete_view, name='schedule_delete'),
    path('unscheduled/', views.unscheduled_patients_view, name='unscheduled_patients'),
    path('mappings/', views.machine_mappings_view, name='machine_mappings'),
    path('mappings/<int:mapping_id>/edit/', views.edit_machine_mapping_view, name='edit_machine_mapping'),
    path('manual-grid/', report_views.manual_schedule_grid_view, name='scheduler_manual_grid'),
    
    # API endpoints
    path('api/machine-options/<int:machine_id>/', views.get_machine_options_api, name='api_machine_options'),
    path('api/nearest-free-slot/', views.get_nearest_free_slot_api, name='api_nearest_free_slot'),
]
