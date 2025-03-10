import random
import logging
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from .models import FileChunk, FileNode, ChunkStatus

logger = logging.getLogger(__name__)


def create_chunk_replicas(min_replicas=1):
    """
    Create replicas of file chunks for redundancy

    Args:
        min_replicas: Minimum number of replicas per chunk
    """
    # Get all non-replica chunks
    original_chunks = FileChunk.objects.filter(
        is_replica=False,
        status=ChunkStatus.UPLOADED
    )

    # Get active nodes
    active_nodes = list(FileNode.objects.filter(status='active'))

    if not active_nodes:
        logger.error("No active nodes available for replication")
        return

    # Create replicas for each chunk if needed
    for chunk in original_chunks:
        # Check existing replicas
        existing_replicas = FileChunk.objects.filter(
            file=chunk.file,
            chunk_number=chunk.chunk_number,
            is_replica=True,
            status=ChunkStatus.UPLOADED
        ).count()

        # Create additional replicas if needed
        replicas_to_create = max(0, min_replicas - existing_replicas)

        for _ in range(replicas_to_create):
            try:
                # Get nodes that don't already have this chunk
                nodes_with_chunk = FileChunk.objects.filter(
                    file=chunk.file,
                    chunk_number=chunk.chunk_number
                ).values_list('node_id', flat=True)

                available_nodes = [node for node in active_nodes if node.id not in nodes_with_chunk]

                if not available_nodes:
                    # If all nodes already have this chunk, pick a random node
                    target_node = random.choice(active_nodes)
                else:
                    # Otherwise, pick from available nodes
                    target_node = random.choice(available_nodes)

                # Read the original chunk
                with default_storage.open(chunk.storage_path, 'rb') as f:
                    chunk_data = f.read()

                # Create a new storage path for the replica
                replica_path = f"replicas/{chunk.file.uploader.username}/{chunk.file.id}_{chunk.chunk_number}_{target_node.id}.chunk"

                # Save the replica
                default_storage.save(replica_path, ContentFile(chunk_data))

                # Create replica record
                FileChunk.objects.create(
                    file=chunk.file,
                    chunk_number=chunk.chunk_number,
                    size_bytes=chunk.size_bytes,
                    checksum=chunk.checksum,
                    storage_path=replica_path,
                    node=target_node,
                    is_replica=True,
                    status=ChunkStatus.UPLOADED
                )

                logger.info(f"Created replica for chunk {chunk.id} on node {target_node.name}")

            except Exception as e:
                logger.error(f"Failed to create replica for chunk {chunk.id}: {str(e)}")


def verify_chunk_integrity():
    """Verify the integrity of all chunks"""
    chunks = FileChunk.objects.filter(status=ChunkStatus.UPLOADED)

    for chunk in chunks:
        if not chunk.verify_integrity():
            logger.warning(f"Chunk {chunk.id} (file: {chunk.file.name}, chunk: {chunk.chunk_number}) is corrupt")