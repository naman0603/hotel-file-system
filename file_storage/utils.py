import os
import hashlib
import uuid
import logging
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.db import transaction
from .models import StoredFile, FileChunk, FileNode, ChunkStatus
from .node_manager import NodeManager
from minio import Minio
from minio.error import S3Error

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
        Split a file into chunks and store them across multiple nodes

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
        nodes = NodeManager.get_active_nodes()
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

                # Select best node for this chunk
                node = NodeManager.elect_best_node_for_upload()
                if not node:
                    raise ChunkingError("No available nodes to store chunk")

                # Create a unique object name for this chunk
                object_name = f"chunks/{user.username}/{stored_file.id}_{chunk_number}_{uuid.uuid4().hex}.chunk"

                try:
                    # Get MinIO client for the selected node
                    client = NodeManager.get_node_client(node)

                    # Upload chunk to MinIO
                    from io import BytesIO
                    client.put_object(
                        bucket_name=node.bucket_name,
                        object_name=object_name,
                        data=BytesIO(chunk_data),
                        length=len(chunk_data)
                    )

                    # Create chunk record
                    chunk = FileChunk.objects.create(
                        file=stored_file,
                        chunk_number=chunk_number,
                        size_bytes=len(chunk_data),
                        checksum=chunk_hash,
                        storage_path=object_name,
                        node=node,
                        status=ChunkStatus.UPLOADED
                    )

                    chunks.append(chunk)
                    chunk_number += 1
                    position += len(chunk_data)
                    logger.info(f"Chunk {chunk_number - 1} uploaded to node {node.name} for file {stored_file.id}")

                except Exception as e:
                    logger.error(f"Error uploading chunk {chunk_number} to node {node.name}: {str(e)}")

                    # Try another node
                    alternative_nodes = [n for n in nodes if n.id != node.id]
                    if alternative_nodes:
                        try:
                            alt_node = alternative_nodes[0]
                            alt_client = NodeManager.get_node_client(alt_node)

                            # Upload to alternative node
                            alt_client.put_object(
                                bucket_name=alt_node.bucket_name,
                                object_name=object_name,
                                data=BytesIO(chunk_data),
                                length=len(chunk_data)
                            )

                            # Create chunk record
                            chunk = FileChunk.objects.create(
                                file=stored_file,
                                chunk_number=chunk_number,
                                size_bytes=len(chunk_data),
                                checksum=chunk_hash,
                                storage_path=object_name,
                                node=alt_node,
                                status=ChunkStatus.UPLOADED
                            )

                            chunks.append(chunk)
                            chunk_number += 1
                            position += len(chunk_data)
                            logger.info(f"Chunk {chunk_number - 1} uploaded to alternative node {alt_node.name}")

                        except Exception as alt_error:
                            logger.error(f"Error uploading to alternative node: {str(alt_error)}")
                            transaction.savepoint_rollback(sid)
                            raise ChunkingError(f"Failed to store chunk {chunk_number} on any available node")
                    else:
                        transaction.savepoint_rollback(sid)
                        raise ChunkingError(f"No alternative nodes available for chunk {chunk_number}")

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

    def reassemble_file_optimized(self, stored_file):
        """
        Reassemble a file from its chunks with multiserver support

        Args:
            stored_file: StoredFile model instance

        Returns:
            file-like object with the reassembled file
        """
        from io import BytesIO

        # Create a buffer to hold the reassembled file
        buffer = BytesIO()

        # Get all chunk numbers for this file
        chunk_numbers = FileChunk.objects.filter(
            file=stored_file,
            status=ChunkStatus.UPLOADED
        ).values_list('chunk_number', flat=True).distinct().order_by('chunk_number')

        if not chunk_numbers:
            logger.error(f"No chunks found for file {stored_file.id}")
            raise ReassemblyError("No chunks found for this file")

        # Expected chunk numbers
        expected_numbers = list(range(1, max(chunk_numbers) + 1))

        # Check for missing chunks
        missing = set(expected_numbers) - set(chunk_numbers)
        if missing:
            logger.error(f"Missing chunks for file {stored_file.id}: {missing}")
            raise ReassemblyError(f"Missing chunks {missing}")

        # Reassemble the file by retrieving each chunk from the appropriate node
        for chunk_number in expected_numbers:
            # Find best node for this chunk
            node = NodeManager.select_node_for_chunk(stored_file.id, chunk_number)
            if not node:
                logger.error(f"No available node found for chunk {chunk_number}")
                raise ReassemblyError(f"No available node for chunk {chunk_number}")

            # Get chunk details
            chunk = FileChunk.objects.filter(
                file=stored_file,
                chunk_number=chunk_number,
                node=node
            ).first()

            if not chunk:
                logger.error(f"Chunk record not found for chunk {chunk_number} on node {node.name}")
                raise ReassemblyError(f"Chunk {chunk_number} record not found")

            try:
                # Get MinIO client for this node
                client = NodeManager.get_node_client(node)

                # Download the chunk
                response = client.get_object(node.bucket_name, chunk.storage_path)
                chunk_data = response.read()

                # Verify integrity
                chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                if chunk_hash != chunk.checksum:
                    logger.warning(f"Checksum mismatch for chunk {chunk.id} from node {node.name}")

                    # Try another node
                    alt_nodes = NodeManager.get_active_nodes()
                    alt_nodes = [n for n in alt_nodes if n.id != node.id]

                    chunk_data = None
                    for alt_node in alt_nodes:
                        alt_chunk = FileChunk.objects.filter(
                            file=stored_file,
                            chunk_number=chunk_number,
                            node=alt_node
                        ).first()

                        if alt_chunk:
                            try:
                                alt_client = NodeManager.get_node_client(alt_node)
                                alt_response = alt_client.get_object(alt_node.bucket_name, alt_chunk.storage_path)
                                alt_data = alt_response.read()

                                alt_hash = hashlib.sha256(alt_data).hexdigest()
                                if alt_hash == alt_chunk.checksum:
                                    chunk_data = alt_data
                                    break
                            except Exception as alt_error:
                                logger.error(
                                    f"Error retrieving from alternative node {alt_node.name}: {str(alt_error)}")

                    if not chunk_data:
                        raise ReassemblyError(f"All copies of chunk {chunk_number} are corrupted or unavailable")

                buffer.write(chunk_data)

            except Exception as e:
                logger.error(f"Error retrieving chunk {chunk_number} from node {node.name}: {str(e)}")

                # Try to retrieve from another node
                try:
                    alt_nodes = NodeManager.get_active_nodes()
                    alt_nodes = [n for n in alt_nodes if n.id != node.id]

                    for alt_node in alt_nodes:
                        alt_chunk = FileChunk.objects.filter(
                            file=stored_file,
                            chunk_number=chunk_number,
                            node=alt_node
                        ).first()

                        if alt_chunk:
                            alt_client = NodeManager.get_node_client(alt_node)
                            alt_response = alt_client.get_object(alt_node.bucket_name, alt_chunk.storage_path)
                            buffer.write(alt_response.read())
                            logger.info(f"Retrieved chunk {chunk_number} from alternative node {alt_node.name}")
                            break
                    else:
                        raise ReassemblyError(f"Failed to retrieve chunk {chunk_number} from any node")

                except Exception as alt_error:
                    logger.error(f"Error retrieving from any alternative node: {str(alt_error)}")
                    raise ReassemblyError(f"Failed to retrieve chunk {chunk_number}")

        # Reset buffer position for reading
        buffer.seek(0)
        return buffer

    def reassemble_file_optimized(self, stored_file):
        """
        Reassemble a file from its chunks with optimized node selection

        Args:
            stored_file: StoredFile model instance

        Returns:
            file-like object with the reassembled file
        """
        from io import BytesIO
        from .retrieval import NodeSelector

        # Create a buffer to hold the reassembled file
        buffer = BytesIO()

        # Get all chunk numbers for this file
        chunk_numbers = FileChunk.objects.filter(
            file=stored_file,
            status=ChunkStatus.UPLOADED
        ).values_list('chunk_number', flat=True).distinct().order_by('chunk_number')

        if not chunk_numbers:
            logger.error(f"No chunks found for file {stored_file.id}")
            raise ReassemblyError("No chunks found for this file")

        # Expected chunk numbers
        expected_numbers = list(range(1, max(chunk_numbers) + 1))

        # Check for missing chunks
        missing = set(expected_numbers) - set(chunk_numbers)
        if missing:
            logger.error(f"Missing chunks for file {stored_file.id}: {missing}")
            raise ReassemblyError(f"Missing chunks {missing}")

        # Reassemble the file by optimally retrieving each chunk
        for chunk_number in expected_numbers:
            chunk, node = NodeSelector.select_node_for_retrieval(stored_file.id, chunk_number)

            if not chunk:
                logger.error(f"Failed to find valid chunk {chunk_number} for file {stored_file.id}")
                raise ReassemblyError(f"Failed to retrieve chunk {chunk_number}")

            try:
                with default_storage.open(chunk.storage_path, 'rb') as f:
                    chunk_data = f.read()

                # Verify chunk integrity
                chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                if chunk_hash != chunk.checksum:
                    logger.warning(f"Checksum mismatch for chunk {chunk.id} from node {node.name}")

                    # Try to get from another node
                    alternative_chunk = FileChunk.objects.filter(
                        file=stored_file,
                        chunk_number=chunk_number,
                        status=ChunkStatus.UPLOADED
                    ).exclude(id=chunk.id).first()

                    if alternative_chunk:
                        with default_storage.open(alternative_chunk.storage_path, 'rb') as f:
                            chunk_data = f.read()

                        # Verify again
                        alt_hash = hashlib.sha256(chunk_data).hexdigest()
                        if alt_hash != alternative_chunk.checksum:
                            raise ReassemblyError(f"All copies of chunk {chunk_number} are corrupted")
                    else:
                        raise ReassemblyError(f"Chunk {chunk_number} is corrupted and no alternatives found")

                buffer.write(chunk_data)

            except IOError as e:
                logger.error(f"IO error reading chunk {chunk.id} from node {node.name}: {str(e)}")
                raise ReassemblyError(f"Failed to read chunk {chunk_number}")

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