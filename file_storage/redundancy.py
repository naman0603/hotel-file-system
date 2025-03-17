import random
import logging
import hashlib
import uuid

from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from .models import FileChunk, FileNode, ChunkStatus, StoredFile
from .node_manager import NodeManager


logger = logging.getLogger(__name__)


class RedundancyManager:
    """Manager class for handling file redundancy and replication"""

    def __init__(self, min_replicas=1):
        self.min_replicas = min_replicas

    # In file_storage/redundancy.py

    def create_replicas_for_chunk(self, chunk, exclude_nodes=None):
        """
        Create replicas for a specific chunk across multiple servers

        Args:
            chunk: FileChunk instance to replicate
            exclude_nodes: list of nodes to exclude (e.g. nodes that already have this chunk)

        Returns:
            int: Number of replicas created
        """
        # Skip if the chunk ID doesn't exist in the database
        if not chunk or not chunk.id:
            logger.warning("Cannot create replicas for non-existent chunk")
            return 0

        # Skip if chunk has no assigned node
        if chunk.node is None:
            logger.warning(f"Cannot create replica for chunk {chunk.id}: source node is None")
            return 0

        # Skip non-original chunks
        if chunk.is_replica:
            return 0

        # Skip chunks that aren't successfully uploaded
        if chunk.status != ChunkStatus.UPLOADED:
            return 0

        if exclude_nodes is None:
            exclude_nodes = []
        else:
            # Ensure exclude_nodes is a list, not a QuerySet
            exclude_nodes = list(exclude_nodes)

        # Add the chunk's current node to exclude list
        if chunk.node and chunk.node not in exclude_nodes:
            exclude_nodes.append(chunk.node)

        # Get list of node IDs to exclude
        exclude_node_ids = [n.id for n in exclude_nodes if n is not None and hasattr(n, 'id')]

        # Get active nodes excluding specified ones
        active_nodes = FileNode.objects.filter(status='active').exclude(id__in=exclude_node_ids)

        if not active_nodes:
            logger.warning(f"No active nodes available for replication of chunk {chunk.id}")
            return 0

        replicas_created = 0

        # Create a replica on each available node, up to min_replicas
        for node in active_nodes[:self.min_replicas]:
            try:
                # Skip if node is None
                if node is None:
                    continue

                # Verify source node exists and is valid
                if chunk.node is None:
                    logger.error(f"Cannot create replica for chunk {chunk.id}: source node is None")
                    continue

                # Get source node client
                source_client = NodeManager.get_node_client(chunk.node)

                if source_client is None:
                    logger.error(
                        f"Cannot create replica for chunk {chunk.id}: failed to get client for source node {chunk.node.name}")
                    continue

                # Get destination node client
                dest_client = NodeManager.get_node_client(node)

                if dest_client is None:
                    logger.error(
                        f"Cannot create replica for chunk {chunk.id}: failed to get client for destination node {node.name}")
                    continue

                # Create a new path for the replica
                replica_path = f"replicas/{chunk.file.uploader.username}/{chunk.file.id}_{chunk.chunk_number}_{uuid.uuid4().hex}.chunk"

                # Copy the object from source to destination
                # First, download from source
                try:
                    response = source_client.get_object(chunk.node.bucket_name, chunk.storage_path)
                    chunk_data = response.read()
                except Exception as e:
                    logger.error(f"Failed to read chunk {chunk.id} from source node: {str(e)}")
                    continue

                # Verify the source integrity
                chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                if chunk_hash != chunk.checksum:
                    logger.error(f"Source chunk {chunk.id} is corrupted, cannot replicate")
                    continue

                # Upload to destination
                from io import BytesIO
                dest_client.put_object(
                    bucket_name=node.bucket_name,
                    object_name=replica_path,
                    data=BytesIO(chunk_data),
                    length=len(chunk_data)
                )

                # Check if replica already exists for this chunk on this node
                existing_replica = FileChunk.objects.filter(
                    file=chunk.file,
                    chunk_number=chunk.chunk_number,
                    is_replica=True,
                    node=node
                ).first()

                if existing_replica:
                    logger.info(f"Replica already exists for chunk {chunk.id} on node {node.name}")
                    continue

                # Create replica record
                FileChunk.objects.create(
                    file=chunk.file,
                    chunk_number=chunk.chunk_number,
                    size_bytes=chunk.size_bytes,
                    checksum=chunk.checksum,
                    storage_path=replica_path,
                    node=node,
                    is_replica=True,
                    status=ChunkStatus.UPLOADED
                )

                replicas_created += 1
                logger.info(f"Created replica for chunk {chunk.id} on node {node.name}")

            except Exception as e:
                node_name = node.name if node else "None"
                logger.error(f"Failed to create replica on node {node_name}: {str(e)}")

        return replicas_created

    def ensure_minimum_replicas(self):
        """
        Ensure all chunks have the minimum number of replicas

        Returns:
            dict: Statistics on replicas created
        """
        stats = {
            'checked': 0,
            'created': 0,
            'failed': 0,
            'already_sufficient': 0
        }

        # Get all original chunks that are not corrupted
        original_chunks = FileChunk.objects.filter(
            is_replica=False,
            status=ChunkStatus.UPLOADED
        )

        for chunk in original_chunks:
            stats['checked'] += 1

            # Count existing replicas
            existing_replicas = FileChunk.objects.filter(
                file=chunk.file,
                chunk_number=chunk.chunk_number,
                is_replica=True,
                status=ChunkStatus.UPLOADED
            ).count()

            if existing_replicas >= self.min_replicas:
                stats['already_sufficient'] += 1
                continue

            # Get nodes that already have this chunk
            nodes_with_chunk = list(FileChunk.objects.filter(
                file=chunk.file,
                chunk_number=chunk.chunk_number
            ).values_list('node', flat=True).distinct())

            nodes_to_exclude = FileNode.objects.filter(id__in=nodes_with_chunk)

            # Create additional replicas
            replicas_to_create = self.min_replicas - existing_replicas

            for _ in range(replicas_to_create):
                result = self.create_replicas_for_chunk(chunk, exclude_nodes=nodes_to_exclude)
                if result > 0:
                    stats['created'] += result
                else:
                    stats['failed'] += 1

        return stats

    def verify_and_repair_all_chunks(self):
        """
        Verify integrity of all chunks and repair if possible

        Returns:
            dict: Statistics on verification and repair
        """
        stats = {
            'verified': 0,
            'corrupt': 0,
            'repaired': 0,
            'unrepairable': 0
        }

        # Get all chunks
        chunks = FileChunk.objects.filter(status=ChunkStatus.UPLOADED)

        for chunk in chunks:
            stats['verified'] += 1

            try:
                # Check if file exists
                if not default_storage.exists(chunk.storage_path):
                    logger.error(f"Chunk file missing for {chunk.id} at {chunk.storage_path}")
                    chunk.status = ChunkStatus.FAILED
                    chunk.save()
                    stats['corrupt'] += 1

                    if self.repair_chunk(chunk):
                        stats['repaired'] += 1
                    else:
                        stats['unrepairable'] += 1
                    continue

                # Verify checksum
                with default_storage.open(chunk.storage_path, 'rb') as f:
                    chunk_data = f.read()

                chunk_hash = hashlib.sha256(chunk_data).hexdigest()

                if chunk_hash != chunk.checksum:
                    logger.error(f"Chunk {chunk.id} checksum mismatch")
                    chunk.status = ChunkStatus.CORRUPT
                    chunk.save()
                    stats['corrupt'] += 1

                    if self.repair_chunk(chunk):
                        stats['repaired'] += 1
                    else:
                        stats['unrepairable'] += 1

            except Exception as e:
                logger.error(f"Error verifying chunk {chunk.id}: {str(e)}")
                chunk.status = ChunkStatus.FAILED
                chunk.save()
                stats['corrupt'] += 1

                if self.repair_chunk(chunk):
                    stats['repaired'] += 1
                else:
                    stats['unrepairable'] += 1

        return stats

    def repair_chunk(self, corrupt_chunk):
        """
        Attempt to repair a corrupted chunk

        Args:
            corrupt_chunk: FileChunk instance that is corrupted

        Returns:
            bool: True if repair successful, False otherwise
        """
        # For replicas, we don't repair - we just create new ones elsewhere
        if corrupt_chunk.is_replica:
            return False

        # For original chunks, try to find a valid replica
        replicas = FileChunk.objects.filter(
            file=corrupt_chunk.file,
            chunk_number=corrupt_chunk.chunk_number,
            is_replica=True,
            status=ChunkStatus.UPLOADED
        )

        for replica in replicas:
            try:
                # Verify replica integrity
                with default_storage.open(replica.storage_path, 'rb') as f:
                    chunk_data = f.read()

                replica_hash = hashlib.sha256(chunk_data).hexdigest()

                if replica_hash == replica.checksum:
                    # Valid replica found, use it to repair
                    with transaction.atomic():
                        # Create a new storage path
                        new_path = f"chunks/{corrupt_chunk.file.uploader.username}/{corrupt_chunk.file.id}_{corrupt_chunk.chunk_number}_{timezone.now().timestamp()}.chunk"

                        # Save as new chunk
                        default_storage.save(new_path, ContentFile(chunk_data))

                        # Update the chunk
                        corrupt_chunk.storage_path = new_path
                        corrupt_chunk.status = ChunkStatus.UPLOADED
                        corrupt_chunk.save()

                        logger.info(f"Repaired chunk {corrupt_chunk.id} from replica {replica.id}")
                        return True

            except Exception as e:
                logger.error(f"Error checking replica {replica.id}: {str(e)}")

        logger.error(f"Unable to repair chunk {corrupt_chunk.id}, no valid replicas found")
        return False

    def check_file_integrity(self, stored_file):
        """
        Check if a file can be fully reassembled

        Args:
            stored_file: StoredFile instance to check

        Returns:
            tuple: (is_intact, missing_chunks, corrupt_chunks)
        """
        # Get all chunks for this file
        chunks = stored_file.chunks.all().order_by('chunk_number')

        if not chunks:
            return False, [], []

        # Check for missing chunks
        chunk_numbers = [chunk.chunk_number for chunk in chunks if not chunk.is_replica]
        expected_numbers = list(range(1, max(chunk_numbers) + 1))
        missing = set(expected_numbers) - set(chunk_numbers)

        # Check for corrupted chunks
        corrupt = [chunk for chunk in chunks
                   if not chunk.is_replica and chunk.status in
                   [ChunkStatus.CORRUPT, ChunkStatus.FAILED]]

        # Check if there are replicas for missing or corrupted chunks
        can_recover = True

        for chunk_num in missing:
            # Check if there's a valid replica
            valid_replica = FileChunk.objects.filter(
                file=stored_file,
                chunk_number=chunk_num,
                is_replica=True,
                status=ChunkStatus.UPLOADED
            ).exists()

            if not valid_replica:
                can_recover = False
                break

        for chunk in corrupt:
            # Check if there's a valid replica
            valid_replica = FileChunk.objects.filter(
                file=stored_file,
                chunk_number=chunk.chunk_number,
                is_replica=True,
                status=ChunkStatus.UPLOADED
            ).exists()

            if not valid_replica:
                can_recover = False
                break

        return can_recover, list(missing), corrupt

    # In file_storage/redundancy.py, add this method to RedundancyManager class

    def create_replica_on_node(self, chunk, target_node):
        """
        Create a replica of a chunk on a specific node

        Args:
            chunk: FileChunk instance to replicate
            target_node: FileNode instance where replica should be created

        Returns:
            bool: True if successful, False otherwise
        """
        # Skip if the chunk is already a replica
        if chunk.is_replica:
            logger.warning(f"Cannot create replica from replica {chunk.id}")
            return False

        # Skip non-uploaded chunks
        if chunk.status != ChunkStatus.UPLOADED:
            logger.warning(f"Cannot create replica for chunk {chunk.id} with status {chunk.status}")
            return False

        # Skip if node is invalid
        if target_node is None or not hasattr(target_node, 'id'):
            logger.error(f"Invalid target node for creating replica of chunk {chunk.id}")
            return False

        # Check if replica already exists
        existing_replica = FileChunk.objects.filter(
            file=chunk.file,
            chunk_number=chunk.chunk_number,
            is_replica=True,
            node=target_node
        ).exists()

        if existing_replica:
            logger.info(f"Replica already exists for chunk {chunk.id} on node {target_node.name}")
            return True

        try:
            # Verify source node
            if chunk.node is None:
                logger.error(f"Source node is None for chunk {chunk.id}")
                return False

            # Get source client
            source_client = NodeManager.get_node_client(chunk.node)
            if source_client is None:
                logger.error(f"Failed to get client for source node {chunk.node.name}")
                return False

            # Get destination client
            dest_client = NodeManager.get_node_client(target_node)
            if dest_client is None:
                logger.error(f"Failed to get client for destination node {target_node.name}")
                return False

            # Read the chunk data
            response = source_client.get_object(chunk.node.bucket_name, chunk.storage_path)
            chunk_data = response.read()

            # Verify integrity
            chunk_hash = hashlib.sha256(chunk_data).hexdigest()
            if chunk_hash != chunk.checksum:
                logger.error(f"Source chunk {chunk.id} is corrupted, cannot replicate")
                return False

            # Create a new path for the replica
            replica_path = f"replicas/{chunk.file.uploader.username}/{chunk.file.id}_{chunk.chunk_number}_{uuid.uuid4().hex}.chunk"

            # Upload to destination
            from io import BytesIO
            dest_client.put_object(
                bucket_name=target_node.bucket_name,
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
                node=target_node,
                is_replica=True,
                status=ChunkStatus.UPLOADED
            )

            logger.info(f"Successfully created replica for chunk {chunk.id} on node {target_node.name}")
            return True

        except Exception as e:
            logger.error(f"Error creating replica on node {target_node.name}: {str(e)}")
            return False