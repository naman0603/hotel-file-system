from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from file_storage.models import FileNode, FileChunk, StoredFile
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Test system resilience when a node is down'

    def add_arguments(self, parser):
        parser.add_argument('file_id', type=str, help='ID of file to test with')
        parser.add_argument('--node', type=str, help='Name of node to simulate as down')

    def handle(self, *args, **options):
        file_id = options['file_id']
        node_name = options.get('node')

        try:
            file = StoredFile.objects.get(id=file_id)
            self.stdout.write(f"Testing with file: {file.name}")

            if node_name:
                node = FileNode.objects.get(name=node_name)
                self.stdout.write(f"Simulating node {node.name} as down")

                # Get all chunks on this node
                primary_chunks = FileChunk.objects.filter(file=file, node=node, is_replica=False)
                self.stdout.write(f"File has {primary_chunks.count()} primary chunks on {node.name}")

                # For each chunk, check if replicas exist on other nodes
                for chunk in primary_chunks:
                    replicas = FileChunk.objects.filter(
                        file=file,
                        chunk_number=chunk.chunk_number,
                        is_replica=True
                    ).exclude(node=node)

                    if replicas.exists():
                        self.stdout.write(
                            f"✅ Chunk {chunk.chunk_number} has {replicas.count()} replicas on other nodes")
                    else:
                        self.stdout.write(f"❌ Chunk {chunk.chunk_number} has NO replicas on other nodes")

            # Overall file resilience check
            chunks = FileChunk.objects.filter(file=file, is_replica=False)
            vulnerable_chunks = 0

            for chunk in chunks:
                replicas = FileChunk.objects.filter(
                    file=file,
                    chunk_number=chunk.chunk_number,
                    is_replica=True
                )
                if replicas.count() == 0:
                    vulnerable_chunks += 1

            if vulnerable_chunks > 0:
                self.stdout.write(self.style.WARNING(f"⚠️ {vulnerable_chunks} chunks have no replicas"))
            else:
                self.stdout.write(self.style.SUCCESS("✅ All chunks have at least one replica"))

        except StoredFile.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"File with ID {file_id} not found"))
        except FileNode.DoesNotExist:
            self.stdout.write(self.style.ERROR(f"Node {node_name} not found"))