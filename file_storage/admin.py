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
    list_display = ('name', 'hostname', 'port', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'hostname')

@admin.register(StoredFile)
class StoredFileAdmin(admin.ModelAdmin):
    list_display = ('name', 'original_filename', 'file_type', 'size_bytes',
                   'upload_date', 'uploader')
    list_filter = ('file_type', 'upload_date')
    search_fields = ('name', 'original_filename', 'uploader__username')
    date_hierarchy = 'upload_date'

@admin.register(FileChunk)
class FileChunkAdmin(admin.ModelAdmin):
    list_display = ('file', 'chunk_number', 'size_bytes', 'node', 'is_replica')
    list_filter = ('is_replica', 'node')
    search_fields = ('file__name',)