from django.urls import path
from . import views

app_name = 'file_storage'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('status/', views.system_status, name='system_status'),
    path('upload/', views.upload_file, name='upload_file'),
    path('download/<uuid:file_id>/', views.download_file, name='download_file'),
    path('file/<uuid:file_id>/', views.file_details, name='file_details'),
]