# file_storage/health.py
import logging
from django.utils import timezone
from .models import FileNode, StoredFile, FileChunk, ChunkStatus

logger = logging.getLogger(__name__)


class SystemHealth:
    """System health monitoring and reporting"""

    @staticmethod
    def get_overall_status():
        """
        Get the overall system health status

        Returns:
            dict: System health status
        """
        # Check node status
        nodes = FileNode.objects.all()
        total_nodes = nodes.count()
        active_nodes = nodes.filter(status='active').count()

        # Check file integrity
        files = StoredFile.objects.all()
        total_files = files.count()

        # Check chunk status
        total_chunks = FileChunk.objects.count()
        corrupt_chunks = FileChunk.objects.filter(status=ChunkStatus.CORRUPT).count()
        failed_chunks = FileChunk.objects.filter(status=ChunkStatus.FAILED).count()

        # Calculate health metrics
        node_health = (active_nodes / total_nodes) * 100 if total_nodes > 0 else 0
        chunk_health = ((
                                    total_chunks - corrupt_chunks - failed_chunks) / total_chunks) * 100 if total_chunks > 0 else 100

        # Determine overall status
        if node_health < 50 or chunk_health < 80:
            status = "critical"
        elif node_health < 75 or chunk_health < 95:
            status = "warning"
        else:
            status = "healthy"

        return {
            'status': status,
            'timestamp': timezone.now().isoformat(),
            'nodes': {
                'total': total_nodes,
                'active': active_nodes,
                'health_percentage': round(node_health, 1)
            },
            'files': {
                'total': total_files
            },
            'chunks': {
                'total': total_chunks,
                'corrupt': corrupt_chunks,
                'failed': failed_chunks,
                'health_percentage': round(chunk_health, 1)
            }
        }

    @staticmethod
    def get_node_health(node):
        """
        Get health status for a specific node

        Args:
            node: FileNode instance

        Returns:
            dict: Node health details
        """
        # For inactive nodes, always report the correct status
        if node.status != 'active':
            return {
                'id': node.id,
                'name': node.name,
                'status': node.status,
                'health_status': "offline",
                'hostname': node.hostname,
                'port': node.port,
                'chunks': {
                    'total': node.stored_chunks.count(),
                    'corrupt': 0,
                    'failed': 0,
                    'health_percentage': 0
                },
                'updated_at': node.updated_at.isoformat()
            }

        # For active nodes, check availability and calculate health
        is_available = True  # For testing, assume available

        # In production, you would check actual availability
        # by connecting to the node's API or service

        # Get chunks on this node
        total_chunks = node.stored_chunks.count()
        corrupt_chunks = node.stored_chunks.filter(status=ChunkStatus.CORRUPT).count()
        failed_chunks = node.stored_chunks.filter(status=ChunkStatus.FAILED).count()

        # Calculate health percentage
        if total_chunks > 0:
            chunk_health = ((total_chunks - corrupt_chunks - failed_chunks) / total_chunks) * 100
        else:
            chunk_health = 100 if is_available else 0

        # Determine node health status
        if not is_available:
            health_status = "critical"
        elif chunk_health < 80:
            health_status = "critical"
        elif chunk_health < 95:
            health_status = "warning"
        else:
            health_status = "healthy"

        return {
            'id': node.id,
            'name': node.name,
            'status': node.status,
            'health_status': health_status,
            'hostname': node.hostname,
            'port': node.port,
            'chunks': {
                'total': total_chunks,
                'corrupt': corrupt_chunks,
                'failed': failed_chunks,
                'health_percentage': round(chunk_health, 1)
            },
            'updated_at': node.updated_at.isoformat()
        }

    @staticmethod
    def get_file_health(stored_file):
        """
        Get health status for a specific file

        Args:
            stored_file: StoredFile instance

        Returns:
            dict: File health details
        """
        # Get all chunks for this file
        chunks = stored_file.chunks.all()
        total_chunks = chunks.filter(is_replica=False).count()
        corrupt_chunks = chunks.filter(is_replica=False, status=ChunkStatus.CORRUPT).count()
        failed_chunks = chunks.filter(is_replica=False, status=ChunkStatus.FAILED).count()

        # Check for missing chunks
        chunk_numbers = [c.chunk_number for c in chunks.filter(is_replica=False)]
        expected_numbers = list(range(1, max(chunk_numbers) + 1)) if chunk_numbers else []
        missing_chunks = set(expected_numbers) - set(chunk_numbers)

        # Calculate health metrics
        chunk_health = ((total_chunks - corrupt_chunks - failed_chunks - len(
            missing_chunks)) / total_chunks) * 100 if total_chunks > 0 else 0

        # Check if file can be recovered using replicas
        can_recover = True
        unrecoverable_chunks = []

        for chunk_num in missing_chunks:
            valid_replica = stored_file.chunks.filter(
                chunk_number=chunk_num,
                is_replica=True,
                status=ChunkStatus.UPLOADED
            ).exists()

            if not valid_replica:
                can_recover = False
                unrecoverable_chunks.append(chunk_num)

        for chunk in chunks.filter(is_replica=False, status__in=[ChunkStatus.CORRUPT, ChunkStatus.FAILED]):
            valid_replica = stored_file.chunks.filter(
                chunk_number=chunk.chunk_number,
                is_replica=True,
                status=ChunkStatus.UPLOADED
            ).exists()

            if not valid_replica:
                can_recover = False
                unrecoverable_chunks.append(chunk.chunk_number)

        # Determine health status
        if not can_recover:
            health_status = "critical"
        elif corrupt_chunks > 0 or failed_chunks > 0 or missing_chunks:
            health_status = "warning"
        else:
            health_status = "healthy"

        return {
            'id': str(stored_file.id),
            'name': stored_file.name,
            'original_filename': stored_file.original_filename,
            'size_bytes': stored_file.size_bytes,
            'can_recover': can_recover,
            'health_status': health_status,
            'chunks': {
                'total': total_chunks,
                'corrupt': corrupt_chunks,
                'failed': failed_chunks,
                'missing': len(missing_chunks),
                'missing_numbers': list(missing_chunks),
                'unrecoverable': unrecoverable_chunks,
                'health_percentage': round(chunk_health, 1)
            },
            'upload_date': stored_file.upload_date.isoformat()
        }