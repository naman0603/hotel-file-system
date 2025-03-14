import random
import logging
import hashlib
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from .models import FileChunk, FileNode, ChunkStatus, StoredFile

logger = logging.getLogger(__name__)


class RedundancyManager:
    """Manager class for handling file redundancy and replication"""

    def __init__(self, min_replicas=1):
        self.min_replicas = min_replicas

    def create_replicas_for_chunk(self, chunk, exclude_nodes=None):
        """
        Create replicas for a specific chunk

        Args:
            chunk: FileChunk instance to replicate
            exclude_nodes: list of nodes to exclude (e.g. nodes that already have this chunk)

        Returns:
            int: Number of replicas created
        """
        if exclude_nodes is None:
            exclude_nodes = []

        # Don't replicate replicas
        if chunk.is_replica:
            return 0

        # Don't replicate corrupted chunks
        if chunk.status != ChunkStatus.UPLOADED:
            return 0

        # Get active nodes excluding specified ones
        active_nodes = FileNode.objects.filter(status='active').exclude(id__in=[n.id for n in exclude_nodes])

        if not active_nodes:
            logger.warning(f"No active nodes available for replication of chunk {chunk.id}")
            return 0

        # Choose a random node for the replica
        try:
            target_node = random.choice(list(active_nodes))

            # Read the original chunk
            with default_storage.open(chunk.storage_path, 'rb') as f:
                chunk_data = f.read()

            # Verify the chunk integrity before replicating
            chunk_hash = hashlib.sha256(chunk_data).hexdigest()
            if chunk_hash != chunk.checksum:
                logger.error(f"Original chunk {chunk.id} is corrupted, cannot replicate")
                chunk.status = ChunkStatus.CORRUPT
                chunk.save()
                return 0

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
            return 1

        except Exception as e:
            logger.error(f"Failed to create replica for chunk {chunk.id}: {str(e)}")
            return 0

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