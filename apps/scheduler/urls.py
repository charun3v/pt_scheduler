from django.urls import path
from . import views

urlpatterns = [
    path('patient/<int:patient_id>/', views.schedule_patient_view, name='schedule_patient'),
    path('unscheduled/', views.unscheduled_patients_view, name='unscheduled_patients'),
    path('mappings/', views.machine_mappings_view, name='machine_mappings'),
    path('mappings/<int:mapping_id>/edit/', views.edit_machine_mapping_view, name='edit_machine_mapping'),
    
    # API endpoints
    path('api/machine-options/<int:machine_id>/', views.get_machine_options_api, name='api_machine_options'),
    path('api/nearest-free-slot/', views.get_nearest_free_slot_api, name='api_nearest_free_slot'),
]
