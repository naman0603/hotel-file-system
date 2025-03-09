from django.db import models
from django.contrib.auth.models import User
import uuid


class FileNode(models.Model):
    """Represents a storage node in the distributed system"""
    name = models.CharField(max_length=100)
    hostname = models.CharField(max_length=255)
    port = models.IntegerField(default=9000)
    status = models.CharField(max_length=20,
                              choices=[('active', 'Active'),
                                       ('inactive', 'Inactive'),
                                       ('maintenance', 'Maintenance')],
                              default='active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.hostname}:{self.port})"


class StoredFile(models.Model):
    """Metadata for files stored in the system"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=100)
    size_bytes = models.BigIntegerField()
    content_type = models.CharField(max_length=100)
    checksum = models.CharField(max_length=64)  # SHA-256 checksum
    upload_date = models.DateTimeField(auto_now_add=True)
    last_accessed = models.DateTimeField(null=True, blank=True)
    uploader = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='uploaded_files')

    def __str__(self):
        return self.name


class FileChunk(models.Model):
    """Represents a chunk of a file in the distributed storage"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.ForeignKey(StoredFile, on_delete=models.CASCADE, related_name='chunks')
    chunk_number = models.IntegerField()
    size_bytes = models.IntegerField()
    checksum = models.CharField(max_length=64)  # SHA-256 checksum
    storage_path = models.CharField(max_length=255)
    node = models.ForeignKey(FileNode, on_delete=models.SET_NULL, null=True, related_name='stored_chunks')
    is_replica = models.BooleanField(default=False)

    class Meta:
        unique_together = ('file', 'chunk_number', 'is_replica')

    def __str__(self):
        return f"{self.file.name} - Chunk {self.chunk_number}"

