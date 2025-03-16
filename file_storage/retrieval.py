import logging
from datetime import datetime, timedelta
from django.utils import timezone
from django.core.cache import cache
from django.conf import settings
from .models import FileNode, FileChunk, ChunkStatus

logger = logging.getLogger(__name__)


# Add this class to file_storage/retrieval.py if not already present
class NodeSelector:
    """Utility for selecting optimal nodes for file retrieval"""

    @staticmethod
    def get_nearest_node(nodes):
        """
        Determine the nearest node based on latency
        For testing, we'll just use the first active node
        """
        active_nodes = [node for node in nodes if node.status == 'active']
        return active_nodes[0] if active_nodes else None

    @staticmethod
    def get_least_loaded_node(nodes):
        """
        Determine the least loaded node based on current chunk count
        """
        active_nodes = [node for node in nodes if node.status == 'active']
        if not active_nodes:
            return None

        # Sort by number of chunks (ascending)
        sorted_nodes = sorted(active_nodes, key=lambda n: n.stored_chunks.count())
        return sorted_nodes[0] if sorted_nodes else None

    @staticmethod
    def select_node_for_retrieval(file_id, chunk_number):
        """
        Select the best node for retrieving a specific chunk
        """
        # Check cache first
        cache_key = f"preferred_node_{file_id}_{chunk_number}"
        preferred_node_id = cache.get(cache_key)

        if preferred_node_id:
            try:
                node = FileNode.objects.get(id=preferred_node_id, status='active')
                chunk = FileChunk.objects.get(
                    file_id=file_id,
                    chunk_number=chunk_number,
                    node=node,
                    status=ChunkStatus.UPLOADED
                )
                return chunk, node
            except (FileNode.DoesNotExist, FileChunk.DoesNotExist):
                cache.delete(cache_key)

        # Try to find a non-replica chunk
        try:
            chunk = FileChunk.objects.get(
                file_id=file_id,
                chunk_number=chunk_number,
                is_replica=False,
                status=ChunkStatus.UPLOADED
            )
            cache.set(cache_key, chunk.node.id, timeout=3600)  # 1 hour
            return chunk, chunk.node
        except FileChunk.DoesNotExist:
            # Fall back to replicas
            replica = FileChunk.objects.filter(
                file_id=file_id,
                chunk_number=chunk_number,
                is_replica=True,
                status=ChunkStatus.UPLOADED
            ).first()

            if replica:
                cache.set(cache_key, replica.node.id, timeout=3600)
                return replica, replica.node

        return None, None


class FileCache:
    """Utility for caching frequently accessed files"""

    @staticmethod
    def cache_file(file_id, file_data):
        """
        Cache a file's data

        Args:
            file_id: UUID of the file
            file_data: Binary data of the file
        """
        cache_key = f"file_cache_{file_id}"

        # Cache the file data (default 1 hour)
        cache.set(cache_key, file_data, timeout=3600)

        # Update access statistics
        access_key = f"file_access_{file_id}"
        access_count = cache.get(access_key, 0)
        cache.set(access_key, access_count + 1, timeout=86400)  # 24 hours

        logger.info(f"Cached file {file_id}, access count: {access_count + 1}")

    @staticmethod
    def get_cached_file(file_id):
        """
        Get a file from cache if available

        Args:
            file_id: UUID of the file

        Returns:
            bytes or None: File data if in cache
        """
        cache_key = f"file_cache_{file_id}"
        file_data = cache.get(cache_key)

        if file_data:
            # Update access count on cache hit
            access_key = f"file_access_{file_id}"
            access_count = cache.get(access_key, 0)
            cache.set(access_key, access_count + 1, timeout=86400)  # 24 hours
            logger.info(f"Cache hit for file {file_id}, access count: {access_count + 1}")

        return file_data

    @staticmethod
    def is_file_cached(file_id):
        """Check if a file is in the cache"""
        cache_key = f"file_cache_{file_id}"
        return cache.get(cache_key) is not None

    @staticmethod
    def get_access_count(file_id):
        """Get the number of times a file has been accessed"""
        access_key = f"file_access_{file_id}"
        return cache.get(access_key, 0)

    @staticmethod
    def get_frequently_accessed_files(min_access_count=5):
        """
        This would identify frequently accessed files for proactive caching
        In a production system, this would be more sophisticated

        This is a placeholder since Django's cache doesn't support easy enumeration
        """
        # In production, you would use a database or specialized cache to track this
        return []