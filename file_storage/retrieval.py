import logging
from datetime import datetime, timedelta
from django.utils import timezone
from django.core.cache import cache
from django.conf import settings
from .models import FileNode, FileChunk, ChunkStatus

logger = logging.getLogger(__name__)


class NodeSelector:
    """Utility for selecting optimal nodes for file retrieval"""

    @staticmethod
    def get_nearest_node(nodes):
        """
        Determine the nearest node based on latency
        In a real distributed system, this would use network metrics

        For this implementation, we'll simulate by returning active nodes
        with the fastest response time
        """
        active_nodes = [node for node in nodes if node.status == 'active']
        if not active_nodes:
            return None

        # In a real implementation, you would measure actual latency
        # For now, we'll sort by hostname (as a placeholder)
        # In production, you'd replace this with actual latency measurements
        sorted_nodes = sorted(active_nodes, key=lambda n: n.hostname)
        return sorted_nodes[0] if sorted_nodes else None

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

        Strategy:
        1. Check if there's a cached preferred node
        2. Try to find a healthy chunk on any node
        3. Fall back to replicas if needed

        Returns:
            tuple: (FileChunk, FileNode) or (None, None) if not found
        """
        # Check cache first for recently used node for this file/chunk
        cache_key = f"preferred_node_{file_id}_{chunk_number}"
        preferred_node_id = cache.get(cache_key)

        if preferred_node_id:
            # Try to get chunk from preferred node
            try:
                node = FileNode.objects.get(id=preferred_node_id, status='active')
                chunk = FileChunk.objects.get(
                    file_id=file_id,
                    chunk_number=chunk_number,
                    node=node,
                    status=ChunkStatus.UPLOADED
                )
                logger.info(f"Using cached preferred node {node.name} for file {file_id} chunk {chunk_number}")
                return chunk, node
            except (FileNode.DoesNotExist, FileChunk.DoesNotExist):
                # Cached node is no longer valid, continue with regular selection
                cache.delete(cache_key)
                pass

        # Try to find a non-replica chunk first
        try:
            chunk = FileChunk.objects.get(
                file_id=file_id,
                chunk_number=chunk_number,
                is_replica=False,
                status=ChunkStatus.UPLOADED
            )
            # Cache this node as preferred for future retrievals
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
                # Cache replica node
                cache.set(cache_key, replica.node.id, timeout=3600)  # 1 hour
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