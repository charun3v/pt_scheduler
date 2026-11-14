from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('', views.dashboard_view, name='dashboard'),
    path('users/', views.user_list_view, name='user_list'),
    path('users/new/', views.user_create_view, name='user_create'),
    path('users/<int:user_id>/edit/', views.user_edit_view, name='user_edit'),
    path('roles/', views.role_dashboard_view, name='role_dashboard'),
    path('validations/', views.validation_config_view, name='validation_config'),
    path('lookups/', views.lookup_manager_view, name='lookup_manager_default'),
    path('lookups/<str:category>/', views.lookup_manager_view, name='lookup_manager'),
    path('lookups/<str:category>/save/', views.lookup_save_view, name='lookup_add'),
    path('lookups/<str:category>/save/<int:item_id>/', views.lookup_save_view, name='lookup_edit'),
    path('lookups/<str:category>/toggle/<int:item_id>/', views.lookup_toggle_active_view, name='lookup_toggle_active'),
    path('lookups/<str:category>/default/<int:item_id>/', views.lookup_set_default_view, name='lookup_set_default'),
    path('lookups/<str:category>/quick-default/', views.lookup_quick_default_view, name='lookup_quick_default'),
    path('lookups/<str:category>/clear-default/', views.lookup_clear_default_view, name='lookup_clear_default'),
]
