from django.urls import path
from . import views, api

app_name = 'file_storage'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('status/', views.system_status, name='system_status'),
    path('upload/', views.upload_file, name='upload_file'),
    path('download/<uuid:file_id>/', views.download_file, name='download_file'),
    path('file/<uuid:file_id>/', views.file_details, name='file_details'),

    # API endpoints
    path('api/nodes/', api.get_node_status, name='get_node_status'),
    path('api/nodes/<int:node_id>/status/', api.update_node_status, name='update_node_status'),
]