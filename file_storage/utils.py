import os
import hashlib
import uuid
import logging
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
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

    def chunk_file(self, file_obj, stored_file, user):
        """
        Split a file into chunks and store them across multiple nodes

        Args:
            file_obj: The uploaded file object
            stored_file: StoredFile model instance
            user: User who uploaded the file

        Returns:
            list: List of created FileChunk instances
        """
        # Reset file pointer
        file_obj.seek(0)

        # Get only active AND available nodes
        active_nodes = list(FileNode.objects.filter(status='active'))
        available_nodes = []

        for node in active_nodes:
            if NodeManager.check_node_availability(node):
                available_nodes.append(node)

        if len(available_nodes) < 2:
            logger.error("Not enough available storage nodes (minimum 2 required)")
            raise ChunkingError("Not enough available storage nodes (minimum 2 required)")

        # Track file position
        position = 0
        chunk_number = 1
        chunks = []  # Initialize as an empty list

        # Read and store file in chunks
        while True:
            chunk_data = file_obj.read(self.chunk_size)
            if not chunk_data:
                break

            # Calculate checksum for this chunk
            chunk_hash = hashlib.sha256(chunk_data).hexdigest()

            # Try to store on each node until one succeeds
            stored = False

            # Try nodes in order of availability
            for node in available_nodes:
                try:
                    object_name = f"chunks/{user.username}/{stored_file.id}_{chunk_number}_{uuid.uuid4().hex}.chunk"
                    client = NodeManager.get_node_client(node)

                    if client is None:
                        logger.error(f"Failed to get client for node {node.name}")
                        continue

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

                    chunks.append(chunk)  # Append to our list
                    stored = True
                    logger.info(f"Chunk {chunk_number} uploaded to node {node.name}")
                    break
                except Exception as e:
                    logger.error(f"Error uploading chunk {chunk_number} to node {node.name}: {str(e)}")
                    continue

            if not stored:
                logger.error(f"Failed to store chunk {chunk_number} on any available node")
                raise ChunkingError(f"Failed to store chunk {chunk_number} on any available node")

            # Create replicas after successful upload, using only available nodes
            try:
                self._create_replicas(chunk, available_nodes)
            except Exception as repl_e:
                logger.error(f"Failed to create replicas for chunk {chunk_number}: {str(repl_e)}")
                # Continue without failing the upload

            # Move to next chunk
            chunk_number += 1
            position += len(chunk_data)

        return chunks

    def _create_replicas(self, chunk, nodes):
        """Create replicas for this chunk on other available nodes"""
        # Skip replicas if we don't have enough nodes
        if len(nodes) <= 1:
            logger.warning("Not enough nodes for replication")
            return

        # Skip if chunk has no node
        if chunk.node is None:
            logger.warning(f"Chunk {chunk.id} has no node assigned, skipping replication")
            return

        # Only create replicas on active nodes that don't already have this chunk
        replica_nodes = []
        for node in nodes:
            if node is not None and node.id != chunk.node.id:
                # Verify node is actually available
                if NodeManager.check_node_availability(node):
                    replica_nodes.append(node)

        if not replica_nodes:
            logger.warning(f"No available nodes for replication of chunk {chunk.id}")
            return

        for replica_node in replica_nodes:
            try:
                # Check if a replica already exists for this node
                existing_replica = FileChunk.objects.filter(
                    file=chunk.file,
                    chunk_number=chunk.chunk_number,
                    is_replica=True,
                    node=replica_node
                ).exists()

                if existing_replica:
                    logger.info(f"Replica already exists on node {replica_node.name} for chunk {chunk.id}")
                    continue

                with transaction.atomic():
                    # Get source client
                    source_client = NodeManager.get_node_client(chunk.node)

                    if source_client is None:
                        logger.error(f"Failed to get client for source node {chunk.node.name}")
                        continue

                    # Read the original chunk
                    response = source_client.get_object(chunk.node.bucket_name, chunk.storage_path)
                    chunk_data = response.read()

                    # Verify checksum
                    chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                    if chunk_hash != chunk.checksum:
                        logger.error(f"Checksum mismatch when reading chunk {chunk.id} for replication")
                        continue

                    # Create replica on target node
                    replica_path = f"replicas/{chunk.file.uploader.username}/{chunk.file.id}_{chunk.chunk_number}_{uuid.uuid4().hex}.chunk"
                    replica_client = NodeManager.get_node_client(replica_node)

                    if replica_client is None:
                        logger.error(f"Failed to get client for replica node {replica_node.name}")
                        continue

                    from io import BytesIO
                    replica_client.put_object(
                        bucket_name=replica_node.bucket_name,
                        object_name=replica_path,
                        data=BytesIO(chunk_data),
                        length=len(chunk_data)
                    )

                    # Create replica record
                    FileChunk.objects.create(
                        file=chunk.file,
                        chunk_number=chunk.chunk_number,
                        size_bytes=chunk.size_bytes,
                        checksum=chunk.checksum,
                        storage_path=replica_path,
                        node=replica_node,
                        is_replica=True,
                        status=ChunkStatus.UPLOADED
                    )

                    logger.info(f"Created replica for chunk {chunk.id} on node {replica_node.name}")
            except Exception as e:
                logger.error(f"Failed to create replica on node {replica_node.name}: {str(e)}")

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

    # In file_storage/utils.py

    # In file_storage/utils.py

    def reassemble_file_optimized(self, stored_file):
        """
        Reassemble a file from its chunks with comprehensive failover support

        This method can recover a file even if multiple nodes are offline
        as long as either original chunks or their replicas are available
        on at least one working node.

        Args:
            stored_file: StoredFile model instance

        Returns:
            file-like object with the reassembled file
        """
        from io import BytesIO
        import time

        # Create a buffer to hold the reassembled file
        buffer = BytesIO()

        # Get all chunk numbers for this file
        chunk_numbers = sorted(
            FileChunk.objects.filter(file=stored_file)
            .values_list('chunk_number', flat=True)
            .distinct()
        )

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

        # Track nodes that have failed during this retrieval
        failed_nodes = []
        start_time = time.time()

        # Reassemble the file
        for chunk_number in expected_numbers:
            # Try to get all available chunks (both primary and replicas) for this chunk number
            all_chunks = list(
                FileChunk.objects.filter(
                    file=stored_file,
                    chunk_number=chunk_number,
                    status=ChunkStatus.UPLOADED
                ).select_related('node')
            )

            if not all_chunks:
                logger.error(f"No chunks (original or replica) found for chunk number {chunk_number}")
                raise ReassemblyError(f"No chunks found for chunk number {chunk_number}")

            # Sort chunks - prioritize non-replicas and use nodes that aren't in failed_nodes
            # This ensures we try primary chunks first, then replicas, and avoid failed nodes
            all_chunks.sort(
                key=lambda c: (
                    c.is_replica,  # Try non-replicas first
                    c.node in failed_nodes if c.node else True,  # Avoid failed nodes
                )
            )

            # Try each chunk until one succeeds
            chunk_retrieved = False
            for chunk in all_chunks:
                # Skip chunks with no node
                if not chunk.node:
                    logger.warning(f"Chunk {chunk.id} has no associated node, skipping")
                    continue

                # Skip chunks on nodes we already know have failed
                if chunk.node in failed_nodes:
                    logger.debug(f"Skipping chunk on known failed node {chunk.node.name}")
                    continue

                try:
                    # Check if node is responsive
                    client = NodeManager.get_node_client(chunk.node)
                    if client is None:
                        logger.warning(f"Could not get client for node {chunk.node.name}, marking as failed")
                        failed_nodes.append(chunk.node)
                        continue

                    # Get the chunk data with timeout
                    try:
                        # Get the chunk data
                        response = client.get_object(chunk.node.bucket_name, chunk.storage_path)
                        chunk_data = response.read()
                    except Exception as e:
                        logger.warning(f"Error reading chunk from node {chunk.node.name}: {str(e)}")
                        failed_nodes.append(chunk.node)
                        continue

                    # Verify integrity
                    chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                    if chunk_hash != chunk.checksum:
                        logger.warning(f"Checksum mismatch for chunk {chunk.id} from node {chunk.node.name}")
                        # If primary is corrupt, update its status
                        if not chunk.is_replica:
                            chunk.status = ChunkStatus.CORRUPT
                            chunk.save()
                        continue

                    # Write to buffer
                    buffer.write(chunk_data)
                    chunk_retrieved = True

                    chunk_type = "replica" if chunk.is_replica else "primary"
                    logger.info(f"Retrieved chunk {chunk_number} ({chunk_type}) from node {chunk.node.name}")
                    break

                except Exception as e:
                    logger.error(f"Error retrieving chunk {chunk_number} from node {chunk.node.name}: {str(e)}")
                    failed_nodes.append(chunk.node)

            if not chunk_retrieved:
                # If we couldn't get this chunk from any node, fail the download
                raise ReassemblyError(
                    f"Failed to retrieve chunk {chunk_number} from any node. "
                    f"Tried {len(all_chunks)} chunks across {len(set(c.node for c in all_chunks if c.node))} nodes."
                )

        end_time = time.time()
        logger.info(f"File {stored_file.id} reassembled in {end_time - start_time:.2f} seconds")

        # Reset buffer position for reading
        buffer.seek(0)
        return buffer

    def get_healthy_nodes(self):
        """Get a list of currently healthy nodes"""
        active_nodes = FileNode.objects.filter(status='active')
        healthy_nodes = []

        for node in active_nodes:
            if NodeManager.check_node_availability(node):
                healthy_nodes.append(node)

        return healthy_nodes

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