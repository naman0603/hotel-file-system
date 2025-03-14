from django.core.management.base import BaseCommand
from file_storage.redundancy import RedundancyManager
from file_storage.models import FileNode, StoredFile, FileChunk, ChunkStatus


class Command(BaseCommand):
    help = 'Run storage maintenance tasks'

    def add_arguments(self, parser):
        parser.add_argument(
            '--replicas',
            type=int,
            default=1,
            help='Minimum number of replicas per chunk'
        )

        parser.add_argument(
            '--verify',
            action='store_true',
            help='Verify and repair chunks'
        )

        parser.add_argument(
            '--node-id',
            type=int,
            help='Verify and repair chunks for a specific node'
        )

        parser.add_argument(
            '--file-id',
            type=str,
            help='Verify and repair chunks for a specific file'
        )

        parser.add_argument(
            '--stats',
            action='store_true',
            help='Show system statistics'
        )

    def handle(self, *args, **options):
        redundancy_manager = RedundancyManager(min_replicas=options['replicas'])

        if options['verify']:
            self.stdout.write('Verifying and repairing chunks...')

            if options['node_id']:
                node = FileNode.objects.get(id=options['node_id'])
                self.stdout.write(f'Verifying chunks on node: {node.name}')
                # Filter chunks by node
                chunks = FileChunk.objects.filter(node=node, status=ChunkStatus.UPLOADED)
                stats = {'verified': 0, 'corrupt': 0, 'repaired': 0, 'unrepairable': 0}

                for chunk in chunks:
                    # Manual verification for this specific node
                    stats['verified'] += 1
                    if not chunk.verify_integrity():
                        stats['corrupt'] += 1
                        if redundancy_manager.repair_chunk(chunk):
                            stats['repaired'] += 1
                        else:
                            stats['unrepairable'] += 1

                self.stdout.write(self.style.SUCCESS(
                    f"Node verification complete: {stats['verified']} verified, "
                    f"{stats['corrupt']} corrupted, {stats['repaired']} repaired, "
                    f"{stats['unrepairable']} unrepairable"
                ))

            elif options['file_id']:
                try:
                    stored_file = StoredFile.objects.get(id=options['file_id'])
                    self.stdout.write(f'Verifying file: {stored_file.name}')

                    # Check file integrity
                    is_intact, missing, corrupt = redundancy_manager.check_file_integrity(stored_file)

                    if is_intact:
                        if not missing and not corrupt:
                            self.stdout.write(self.style.SUCCESS("File is intact and can be fully reassembled"))
                        else:
                            self.stdout.write(self.style.WARNING(
                                f"File has issues but can be reassembled using replicas. "
                                f"Missing chunks: {len(missing)}, Corrupted chunks: {len(corrupt)}"
                            ))
                    else:
                        self.stdout.write(self.style.ERROR(
                            f"File integrity check failed. Cannot reassemble the file. "
                            f"Missing chunks: {len(missing)}, Corrupted chunks: {len(corrupt)}"
                        ))

                except StoredFile.DoesNotExist:
                    self.stdout.write(self.style.ERROR(f"File with ID {options['file_id']} not found"))

            else:
                # Verify all chunks
                stats = redundancy_manager.verify_and_repair_all_chunks()
                self.stdout.write(self.style.SUCCESS(
                    f"Verification complete: {stats['verified']} verified, "
                    f"{stats['corrupt']} corrupted, {stats['repaired']} repaired, "
                    f"{stats['unrepairable']} unrepairable"
                ))

        # Create replicas if needed
        self.stdout.write(f'Ensuring minimum {options["replicas"]} replicas per chunk...')
        replica_stats = redundancy_manager.ensure_minimum_replicas()
        self.stdout.write(self.style.SUCCESS(
            f"Replica creation complete: {replica_stats['checked']} chunks checked, "
            f"{replica_stats['created']} replicas created, {replica_stats['failed']} failed, "
            f"{replica_stats['already_sufficient']} already had sufficient replicas"
        ))

        if options['stats']:
            self.show_system_stats()

    def show_system_stats(self):
        """Display system statistics"""
        self.stdout.write("\n--- System Statistics ---")

        # Node stats
        nodes = FileNode.objects.all()
        active_nodes = nodes.filter(status='active').count()

        self.stdout.write(f"Total Nodes: {nodes.count()}")
        self.stdout.write(f"Active Nodes: {active_nodes}")

        # File stats
        total_files = StoredFile.objects.count()
        total_size = StoredFile.objects.all().values_list('size_bytes', flat=True)
        total_size_bytes = sum(total_size) if total_size else 0

        self.stdout.write(f"Total Files: {total_files}")
        self.stdout.write(f"Total Storage Size: {self.format_size(total_size_bytes)}")

        # Chunk stats
        total_chunks = FileChunk.objects.count()
        original_chunks = FileChunk.objects.filter(is_replica=False).count()
        replica_chunks = FileChunk.objects.filter(is_replica=True).count()

        self.stdout.write(f"Total Chunks: {total_chunks}")
        self.stdout.write(f"Original Chunks: {original_chunks}")
        self.stdout.write(f"Replica Chunks: {replica_chunks}")

        # Status stats
        corrupt_chunks = FileChunk.objects.filter(status=ChunkStatus.CORRUPT).count()
        failed_chunks = FileChunk.objects.filter(status=ChunkStatus.FAILED).count()

        self.stdout.write(f"Corrupted Chunks: {corrupt_chunks}")
        self.stdout.write(f"Failed Chunks: {failed_chunks}")

    def format_size(self, size_bytes):
        """Format bytes to human-readable form"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024:
                return f"{size_bytes:.2f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.2f} PB"