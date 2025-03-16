import logging
import requests
import time
from django.conf import settings
from django.core.cache import cache
from .models import FileNode, ChunkStatus

logger = logging.getLogger(__name__)


class NodeManager:
    """Manages a cluster of storage nodes"""



    @staticmethod
    def get_primary_node():
        """Get the primary node or elect a new one if needed"""
        primary = FileNode.objects.filter(is_primary=True, status='active').first()
        if primary:
            return primary

        # If no primary exists, elect one based on priority
        candidate = FileNode.objects.filter(status='active').order_by('priority').first()
        if candidate:
            candidate.is_primary = True
            candidate.save()
            logger.info(f"Elected new primary node: {candidate.name}")
            return candidate

        logger.error("No active nodes available to elect as primary")
        return None

    @staticmethod
    def get_active_nodes():
        """Get all active nodes in the cluster"""
        # Always work with a list, not a QuerySet
        return list(FileNode.objects.filter(status='active').order_by('priority'))

    @staticmethod
    def check_node_availability(node):
        """Check if a node is available by pinging its health endpoint"""
        try:
            url = f"http://{node.hostname}:{node.port}/minio/health/ready"
            logger.info(f"Checking availability of node {node.name} at {url}")

            response = requests.get(url, timeout=3)
            is_available = response.status_code == 200

            if is_available:
                logger.info(f"Node {node.name} is available")
            else:
                logger.warning(f"Node {node.name} returned status code {response.status_code}")

            return is_available
        except requests.RequestException as e:
            logger.warning(f"Node {node.name} health check failed: {str(e)}")
            # During development, you might want to consider nodes available even if the health check fails
            return False

    @staticmethod
    def get_node_client(node):
        """Get a MinIO client for a specific node"""
        from minio import Minio

        # Handle None node
        if node is None:
            logger.error("Cannot create MinIO client for None node")
            return None

        # Use appropriate hostname based on environment
        hostname = node.hostname
        port = node.port

        logger.info(f"Connecting to MinIO at {hostname}:{port}")

        try:
            client = Minio(
                f"{hostname}:{port}",
                access_key=node.access_key,
                secret_key=node.secret_key,
                secure=False  # Use True for HTTPS
            )

            # Ensure bucket exists
            if not client.bucket_exists(node.bucket_name):
                client.make_bucket(node.bucket_name)
                logger.info(f"Created bucket {node.bucket_name} on {node.name}")

            return client
        except Exception as e:
            logger.error(f"Error creating MinIO client for node {node.name}: {str(e)}")
            return None

    @staticmethod
    def elect_best_node_for_upload():
        """Find the best node for uploading a new file chunk"""
        nodes = NodeManager.get_active_nodes()
        if not nodes:
            return None

        # Use cache to avoid hitting all nodes for every request
        cache_key = "node_load_stats"
        node_stats = cache.get(cache_key)

        if not node_stats or time.time() - node_stats['timestamp'] > 60:  # Cache for 1 minute
            # Get load statistics for each node
            node_stats = {'timestamp': time.time(), 'nodes': {}}

            for node in nodes:
                try:
                    # In a real implementation, you'd query each node for its current load
                    # For simplicity, we'll use the stored chunk count
                    chunk_count = node.stored_chunks.count()
                    node_stats['nodes'][node.id] = {
                        'chunk_count': chunk_count,
                        'available': NodeManager.check_node_availability(node)
                    }
                except Exception as e:
                    logger.error(f"Error getting stats for node {node.name}: {str(e)}")
                    node_stats['nodes'][node.id] = {
                        'chunk_count': float('inf'),  # Avoid selecting this node
                        'available': False
                    }

            cache.set(cache_key, node_stats, 60)  # Cache for 1 minute

        # Find least loaded available node
        best_node = None
        min_load = float('inf')

        for node in nodes:
            stats = node_stats['nodes'].get(node.id, {'chunk_count': float('inf'), 'available': False})
            if stats['available'] and stats['chunk_count'] < min_load:
                best_node = node
                min_load = stats['chunk_count']

        # If no node is available, try primary node as fallback
        if not best_node:
            primary = NodeManager.get_primary_node()
            if primary and NodeManager.check_node_availability(primary):
                return primary
            return None

        return best_node

    @staticmethod
    def select_node_for_chunk(file_id, chunk_number, exclude_nodes=None):
        """Select best node for a specific chunk"""
        from .models import FileChunk

        if exclude_nodes is None:
            exclude_nodes = []

        # Try to find an existing non-replica chunk
        existing_chunk = FileChunk.objects.filter(
            file_id=file_id,
            chunk_number=chunk_number,
            is_replica=False,
            status=ChunkStatus.UPLOADED
        ).first()

        if existing_chunk and existing_chunk.node and existing_chunk.node.status == 'active':
            if existing_chunk.node.id not in [n.id for n in exclude_nodes]:
                if NodeManager.check_node_availability(existing_chunk.node):
                    return existing_chunk.node

        # Try to find a replica
        replica_chunk = FileChunk.objects.filter(
            file_id=file_id,
            chunk_number=chunk_number,
            is_replica=True,
            status=ChunkStatus.UPLOADED
        ).exclude(node__id__in=[n.id for n in exclude_nodes]).first()

        if replica_chunk and replica_chunk.node and replica_chunk.node.status == 'active':
            if NodeManager.check_node_availability(replica_chunk.node):
                return replica_chunk.node

        # If no suitable node found, elect a new one
        return NodeManager.elect_best_node_for_upload()

    @staticmethod
    def get_node_client(node):
        """Get a MinIO client for a specific node"""
        from minio import Minio

        # Use appropriate hostname based on environment
        hostname = node.hostname
        port = node.port

        logger.info(f"Connecting to MinIO at {hostname}:{port}")

        client = Minio(
            f"{hostname}:{port}",
            access_key=node.access_key,
            secret_key=node.secret_key,
            secure=False  # Use True for HTTPS
        )

        # Ensure bucket exists
        try:
            if not client.bucket_exists(node.bucket_name):
                client.make_bucket(node.bucket_name)
                logger.info(f"Created bucket {node.bucket_name} on {node.name}")
        except Exception as e:
            logger.error(f"Error ensuring bucket on {node.name}: {str(e)}")
            # Don't raise the exception - we'll handle it at higher levels

        return client

    # In file_storage/node_manager.py

    @staticmethod
    def get_available_nodes_count():
        """
        Count how many nodes are currently available and responsive

        Returns:
            int: Number of available nodes
        """
        active_nodes = FileNode.objects.filter(status='active')
        available_count = 0

        for node in active_nodes:
            if NodeManager.check_node_availability(node):
                available_count += 1

        return available_count