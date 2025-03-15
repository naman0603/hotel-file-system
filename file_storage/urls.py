from django.urls import path
from . import views, api, admin_views

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

    # New analytics and caching routes
    path('analytics/', views.analytics_dashboard, name='analytics_dashboard'),
    path('cache/<uuid:file_id>/', views.cache_file, name='cache_file'),

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

    # Admin views
    path('admin/dashboard/', admin_views.admin_dashboard, name='admin_dashboard'),
    path('admin/nodes/', admin_views.admin_node_management, name='node_management'),
    path('admin/storage-report/', admin_views.admin_storage_report, name='storage_report'),
    path('admin/maintenance/', admin_views.admin_system_maintenance, name='system_maintenance'),
    path('admin/ajax/node-status/', admin_views.ajax_node_status, name='ajax_node_status'),

    path('files/', views.file_list, name='file_list'),
    path('files/<uuid:file_id>/', views.enhanced_file_details, name='enhanced_file_details'),
    path('accounts/logout/', views.logout_view, name='logout'),
    path('distributed/', views.distributed_dashboard, name='distributed_dashboard'),

]