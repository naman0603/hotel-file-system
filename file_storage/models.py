import os
import hashlib
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


# Update or add the ChunkStatus model
class ChunkStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    UPLOADING = 'uploading', 'Uploading'
    UPLOADED = 'uploaded', 'Uploaded'
    FAILED = 'failed', 'Failed'
    CORRUPT = 'corrupt', 'Corrupt'


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
    status = models.CharField(
        max_length=20,
        choices=ChunkStatus.choices,
        default=ChunkStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('file', 'chunk_number', 'is_replica')
        ordering = ['chunk_number']

    def __str__(self):
        return f"{self.file.name} - Chunk {self.chunk_number}"

    def verify_integrity(self):
        """Verify the chunk's integrity by comparing checksums"""
        try:
            from django.core.files.storage import default_storage

            # Calculate current checksum
            file_hash = hashlib.sha256()
            with default_storage.open(self.storage_path, 'rb') as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    file_hash.update(byte_block)
            calculated_checksum = file_hash.hexdigest()

            # Compare with stored checksum
            if calculated_checksum == self.checksum:
                return True
            else:
                self.status = ChunkStatus.CORRUPT
                self.save()
                return False
        except Exception as e:
            self.status = ChunkStatus.FAILED
            self.save()
            return False