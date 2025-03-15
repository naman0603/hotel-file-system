from django.contrib import admin
from django.urls import path
from .models import FileNode, StoredFile, FileChunk
from . import admin_views


class FileStorageAdminSite(admin.AdminSite):
    """Custom admin site with additional views"""

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('dashboard/', self.admin_view(admin_views.admin_dashboard), name='admin_dashboard'),
            path('nodes/', self.admin_view(admin_views.admin_node_management), name='node_management'),
            path('storage-report/', self.admin_view(admin_views.admin_storage_report), name='storage_report'),
            path('maintenance/', self.admin_view(admin_views.admin_system_maintenance), name='system_maintenance'),
            path('ajax/node-status/', self.admin_view(admin_views.ajax_node_status), name='ajax_node_status'),
        ]
        return custom_urls + urls


# Uncomment to replace the default admin site
# admin_site = FileStorageAdminSite(name='file_storage_admin')
# admin.site = admin_site

@admin.register(FileNode)
class FileNodeAdmin(admin.ModelAdmin):
    list_display = ('name', 'hostname', 'port', 'status', 'created_at', 'chunk_count')
    list_filter = ('status',)
    search_fields = ('name', 'hostname')

    def chunk_count(self, obj):
        return obj.stored_chunks.count()

    chunk_count.short_description = 'Stored Chunks'


@admin.register(StoredFile)
class StoredFileAdmin(admin.ModelAdmin):
    list_display = ('name', 'original_filename', 'file_type', 'size_bytes',
                    'chunk_count', 'upload_date', 'uploader')
    list_filter = ('file_type', 'upload_date')
    search_fields = ('name', 'original_filename', 'uploader__username')
    date_hierarchy = 'upload_date'

    def chunk_count(self, obj):
        return obj.chunks.count()

    chunk_count.short_description = 'Chunks'


@admin.register(FileChunk)
class FileChunkAdmin(admin.ModelAdmin):
    list_display = ('file', 'chunk_number', 'size_bytes', 'node', 'is_replica', 'status')
    list_filter = ('is_replica', 'status', 'node')
    search_fields = ('file__name', 'file__original_filename')
    actions = ['verify_chunk_integrity']

    def verify_chunk_integrity(self, request, queryset):
        corrupted = 0
        verified = 0

        for chunk in queryset:
            if chunk.verify_integrity():
                verified += 1
            else:
                corrupted += 1

        if corrupted:
            self.message_user(
                request,
                f"{verified} chunks verified, {corrupted} chunks corrupted",
                level='warning'
            )
        else:
            self.message_user(
                request,
                f"All {verified} chunks verified successfully",
                level='success'
            )

    verify_chunk_integrity.short_description = "Verify integrity of selected chunks"