import os
import hashlib
import uuid
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from .models import StoredFile, FileChunk, FileNode, ChunkStatus


class FileChunker:
    """Utility class for handling file chunking operations"""

    def __init__(self, chunk_size=5 * 1024 * 1024):  # Default 5MB chunks
        self.chunk_size = chunk_size

    def chunk_file(self, file_obj, stored_file, user):
        """
        Split a file into chunks and store them

        Args:
            file_obj: The uploaded file object
            stored_file: StoredFile model instance
            user: User who uploaded the file

        Returns:
            list: List of created FileChunk instances
        """
        # Reset file pointer
        file_obj.seek(0)

        # Get active storage nodes
        nodes = list(FileNode.objects.filter(status='active'))
        if not nodes:
            raise Exception("No active storage nodes available")

        # Track file position
        position = 0
        chunk_number = 1
        chunks = []

        # Read and store file in chunks
        while True:
            chunk_data = file_obj.read(self.chunk_size)
            if not chunk_data:
                break

            # Calculate checksum for this chunk
            chunk_hash = hashlib.sha256(chunk_data).hexdigest()

            # Select a node (simple round-robin)
            node = nodes[(chunk_number - 1) % len(nodes)]

            # Generate a unique filename for this chunk
            chunk_filename = f"{stored_file.id}_{chunk_number}_{uuid.uuid4().hex}.chunk"
            storage_path = f"chunks/{user.username}/{chunk_filename}"

            # Store the chunk
            default_storage.save(storage_path, ContentFile(chunk_data))

            # Create chunk record
            chunk = FileChunk.objects.create(
                file=stored_file,
                chunk_number=chunk_number,
                size_bytes=len(chunk_data),
                checksum=chunk_hash,
                storage_path=storage_path,
                node=node,
                status=ChunkStatus.UPLOADED
            )

            chunks.append(chunk)
            chunk_number += 1
            position += len(chunk_data)

        return chunks

    def reassemble_file(self, stored_file):
        """
        Reassemble a file from its chunks

        Args:
            stored_file: StoredFile model instance

        Returns:
            file-like object with the reassembled file
        """
        from io import BytesIO

        # Get all chunks ordered by chunk_number
        chunks = stored_file.chunks.filter(status=ChunkStatus.UPLOADED).order_by('chunk_number')

        if not chunks:
            raise Exception("No valid chunks found for this file")

        # Create a buffer to hold the reassembled file
        buffer = BytesIO()

        # Read each chunk and write to buffer
        for chunk in chunks:
            try:
                with default_storage.open(chunk.storage_path, 'rb') as f:
                    buffer.write(f.read())
            except Exception as e:
                # Try to find a replica if available
                replica = FileChunk.objects.filter(
                    file=stored_file,
                    chunk_number=chunk.chunk_number,
                    is_replica=True,
                    status=ChunkStatus.UPLOADED
                ).first()

                if replica:
                    with default_storage.open(replica.storage_path, 'rb') as f:
                        buffer.write(f.read())
                else:
                    raise Exception(f"Chunk {chunk.chunk_number} is corrupted and no replica found")

        # Reset buffer position for reading
        buffer.seek(0)
        return buffer