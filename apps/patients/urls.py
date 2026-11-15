from django.urls import path
from . import views

urlpatterns = [
    path('register/', views.patient_register_view, name='patient_register'),
    path('search/', views.patient_search_view, name='patient_search'),
    path('<int:patient_id>/', views.patient_detail_view, name='patient_detail'),
    path('<int:patient_id>/edit/', views.patient_edit_view, name='patient_edit'),
    path('<int:patient_id>/delete/', views.patient_delete_view, name='patient_delete'),
    path('<int:patient_id>/hide-dashboard/', views.patient_hide_dashboard_view, name='patient_hide_dashboard'),
    path('<int:patient_id>/unhide-dashboard/', views.patient_unhide_dashboard_view, name='patient_unhide_dashboard'),
]
