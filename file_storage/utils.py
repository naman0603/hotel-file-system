import os
import hashlib
import uuid
import logging
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.db import transaction
from .models import StoredFile, FileChunk, FileNode, ChunkStatus

logger = logging.getLogger(__name__)


class ChunkingError(Exception):
    """Exception raised for errors during file chunking process"""
    pass


class ReassemblyError(Exception):
    """Exception raised for errors during file reassembly process"""
    pass


class FileChunker:
    """Utility class for handling file chunking operations"""

    def __init__(self, chunk_size=5 * 1024 * 1024):  # Default 5MB chunks
        self.chunk_size = chunk_size

    @transaction.atomic
    def chunk_file(self, file_obj, stored_file, user):
        """
        Split a file into chunks and store them with transaction support

        Args:
            file_obj: The uploaded file object
            stored_file: StoredFile model instance
            user: User who uploaded the file

        Returns:
            list: List of created FileChunk instances

        Raises:
            ChunkingError: If chunking process fails
        """
        # Reset file pointer
        file_obj.seek(0)

        # Get active storage nodes
        nodes = list(FileNode.objects.filter(status='active'))
        if not nodes:
            logger.error("No active storage nodes available")
            raise ChunkingError("No active storage nodes available for storing file chunks")

        # Track file position
        position = 0
        chunk_number = 1
        chunks = []

        try:
            # Begin a savepoint for rollback if needed
            sid = transaction.savepoint()

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

                try:
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
                    logger.info(f"Chunk {chunk_number - 1} uploaded successfully for file {stored_file.id}")

                except Exception as e:
                    logger.error(f"Error uploading chunk {chunk_number} for file {stored_file.id}: {str(e)}")
                    # Roll back if there's an error
                    transaction.savepoint_rollback(sid)
                    raise ChunkingError(f"Failed to store chunk {chunk_number}: {str(e)}")

            # Commit the transaction
            transaction.savepoint_commit(sid)
            return chunks

        except Exception as e:
            # This handles any other exceptions that might occur
            logger.error(f"Unexpected error during chunking for file {stored_file.id}: {str(e)}")
            if 'sid' in locals():
                transaction.savepoint_rollback(sid)
            # Clean up any created files
            self._cleanup_partial_upload(chunks)
            raise ChunkingError(f"File chunking failed: {str(e)}")

    def _cleanup_partial_upload(self, chunks):
        """Clean up storage after a failed upload"""
        for chunk in chunks:
            try:
                # Delete the file from storage
                if default_storage.exists(chunk.storage_path):
                    default_storage.delete(chunk.storage_path)
                # Delete the database record
                chunk.delete()
            except Exception as e:
                logger.error(f"Error cleaning up chunk {chunk.id}: {str(e)}")

    def reassemble_file(self, stored_file):
        """
        Reassemble a file from its chunks with error handling

        Args:
            stored_file: StoredFile model instance

        Returns:
            file-like object with the reassembled file

        Raises:
            ReassemblyError: If reassembly process fails
        """
        from io import BytesIO

        # Get all chunks ordered by chunk_number
        chunks = stored_file.chunks.filter(status=ChunkStatus.UPLOADED).order_by('chunk_number')

        if not chunks:
            logger.error(f"No valid chunks found for file {stored_file.id}")
            raise ReassemblyError("No valid chunks found for this file")

        # Check if we have all chunks
        chunk_numbers = [chunk.chunk_number for chunk in chunks]
        expected_numbers = list(range(1, max(chunk_numbers) + 1))

        if sorted(chunk_numbers) != expected_numbers:
            missing = set(expected_numbers) - set(chunk_numbers)
            logger.error(f"Missing chunks for file {stored_file.id}: {missing}")

            # Try to recover from replicas
            recovered = self._recover_missing_chunks(stored_file, missing)
            if not recovered:
                raise ReassemblyError(f"Missing chunks {missing} and unable to recover from replicas")

            # Get updated chunks list after recovery
            chunks = stored_file.chunks.filter(status=ChunkStatus.UPLOADED).order_by('chunk_number')

        # Create a buffer to hold the reassembled file
        buffer = BytesIO()

        # Read each chunk and write to buffer
        for chunk in chunks:
            try:
                with default_storage.open(chunk.storage_path, 'rb') as f:
                    chunk_data = f.read()

                # Verify chunk integrity
                chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                if chunk_hash != chunk.checksum:
                    logger.warning(f"Checksum mismatch for chunk {chunk.id}, trying replica")

                    # Try to find a replica
                    chunk_data = self._get_replica_data(stored_file, chunk.chunk_number)
                    if not chunk_data:
                        raise ReassemblyError(f"Chunk {chunk.chunk_number} is corrupted and no valid replica found")

                buffer.write(chunk_data)

            except IOError as e:
                logger.error(f"IO error reading chunk {chunk.id}: {str(e)}")

                # Try to get data from replica
                chunk_data = self._get_replica_data(stored_file, chunk.chunk_number)
                if not chunk_data:
                    raise ReassemblyError(f"Failed to read chunk {chunk.chunk_number} and no valid replica found")

                buffer.write(chunk_data)

        # Reset buffer position for reading
        buffer.seek(0)
        return buffer

    def _recover_missing_chunks(self, stored_file, missing_numbers):
        """Attempt to recover missing chunks from replicas"""
        all_recovered = True

        for chunk_number in missing_numbers:
            # Try to find a replica
            replica = FileChunk.objects.filter(
                file=stored_file,
                chunk_number=chunk_number,
                is_replica=True,
                status=ChunkStatus.UPLOADED
            ).first()

            if replica:
                # Create a new primary chunk from the replica
                try:
                    with default_storage.open(replica.storage_path, 'rb') as f:
                        chunk_data = f.read()

                    # Verify replica integrity
                    chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                    if chunk_hash != replica.checksum:
                        logger.error(f"Replica for chunk {chunk_number} is corrupted")
                        all_recovered = False
                        continue

                    # Store as a new primary chunk
                    chunk_filename = f"{stored_file.id}_{chunk_number}_{uuid.uuid4().hex}.chunk"
                    storage_path = f"chunks/{stored_file.uploader.username}/{chunk_filename}"

                    default_storage.save(storage_path, ContentFile(chunk_data))

                    # Create new chunk record
                    FileChunk.objects.create(
                        file=stored_file,
                        chunk_number=chunk_number,
                        size_bytes=replica.size_bytes,
                        checksum=replica.checksum,
                        storage_path=storage_path,
                        node=replica.node,
                        is_replica=False,
                        status=ChunkStatus.UPLOADED
                    )

                    logger.info(f"Recovered chunk {chunk_number} from replica for file {stored_file.id}")

                except Exception as e:
                    logger.error(f"Failed to recover chunk {chunk_number} from replica: {str(e)}")
                    all_recovered = False
            else:
                logger.error(f"No replica found for chunk {chunk_number} of file {stored_file.id}")
                all_recovered = False

        return all_recovered

    def _get_replica_data(self, stored_file, chunk_number):
        """Get chunk data from a replica if available"""
        replicas = FileChunk.objects.filter(
            file=stored_file,
            chunk_number=chunk_number,
            is_replica=True,
            status=ChunkStatus.UPLOADED
        )

        for replica in replicas:
            try:
                with default_storage.open(replica.storage_path, 'rb') as f:
                    chunk_data = f.read()

                # Verify integrity
                chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                if chunk_hash == replica.checksum:
                    logger.info(f"Successfully retrieved data from replica for chunk {chunk_number}")
                    return chunk_data
                else:
                    logger.warning(f"Replica {replica.id} for chunk {chunk_number} is corrupted")

            except Exception as e:
                logger.error(f"Error reading replica {replica.id}: {str(e)}")

        logger.error(f"All replicas for chunk {chunk_number} are invalid or corrupt")
        return None