from django.contrib import admin
from .models import FileNode, StoredFile, FileChunk

# First, unregister if already registered (to prevent errors)
try:
    admin.site.unregister(FileNode)
except admin.sites.NotRegistered:
    pass

try:
    admin.site.unregister(StoredFile)
except admin.sites.NotRegistered:
    pass

try:
    admin.site.unregister(FileChunk)
except admin.sites.NotRegistered:
    pass

# Now register with your custom admin classes
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