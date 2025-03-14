from django.urls import path
from . import views, api

app_name = 'file_storage'

urlpatterns = [

    path('', views.dashboard, name='dashboard'),
    path('status/', views.system_status, name='system_status'),
    path('upload/', views.upload_file, name='upload_file'),
    path('download/<uuid:file_id>/', views.download_file, name='download_file'),
    path('file/<uuid:file_id>/', views.file_details, name='file_details'),

    # New routes for health monitoring and repair
    path('health/', views.health_dashboard, name='health_dashboard'),
    path('repair/<str:file_id>/', views.repair_file, name='repair_file'),

    # API endpoints
    path('api/nodes/', api.get_node_status, name='get_node_status'),
    path('api/nodes/<int:node_id>/status/', api.update_node_status, name='update_node_status'),

    # Health check API endpoints
    path('api/health/', api.system_health, name='system_health'),
    path('api/health/nodes/', api.all_nodes_health, name='all_nodes_health'),
    path('api/health/nodes/<int:node_id>/', api.node_health, name='node_health'),
    path('api/health/files/', api.user_files_health, name='user_files_health'),
    path('api/health/files/<str:file_id>/', api.file_health, name='file_health'),
    path('api/admin/health/', api.admin_system_health, name='admin_system_health'),
]