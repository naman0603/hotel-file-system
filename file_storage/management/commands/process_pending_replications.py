# In file_storage/management/commands/process_pending_replications.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from file_storage.models import PendingReplication, FileChunk, ChunkStatus
from file_storage.node_manager import NodeManager
from file_storage.redundancy import RedundancyManager
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Process pending replications for chunks to offline nodes'

    def add_arguments(self, parser):
        parser.add_argument(
            '--max-attempts',
            type=int,
            default=5,
            help='Maximum attempts before marking a replication as failed'
        )

    def handle(self, *args, **options):
        max_attempts = options['max_attempts']

        # Get all pending replications
        pending = PendingReplication.objects.all().select_related('chunk', 'target_node')
        self.stdout.write(f"Found {pending.count()} pending replications")

        processed = 0
        failed = 0
        skipped = 0

        for pending_rep in pending:
            # Skip if too many attempts
            if pending_rep.attempts >= max_attempts:
                self.stdout.write(self.style.WARNING(
                    f"Skipping replication to {pending_rep.target_node.name} - max attempts reached"
                ))
                skipped += 1
                continue

            # Check if node is online
            if not NodeManager.check_node_availability(pending_rep.target_node):
                self.stdout.write(f"Node {pending_rep.target_node.name} still offline, skipping")
                pending_rep.attempts += 1
                pending_rep.last_attempt = timezone.now()
                pending_rep.save()
                skipped += 1
                continue

            # Attempt replication
            try:
                redundancy_manager = RedundancyManager()
                success = redundancy_manager.create_replica_on_node(
                    pending_rep.chunk,
                    pending_rep.target_node
                )

                if success:
                    self.stdout.write(self.style.SUCCESS(
                        f"Successfully created replica on {pending_rep.target_node.name}"
                    ))
                    pending_rep.delete()  # Remove from pending
                    processed += 1
                else:
                    self.stdout.write(self.style.ERROR(
                        f"Failed to create replica on {pending_rep.target_node.name}"
                    ))
                    pending_rep.attempts += 1
                    pending_rep.last_attempt = timezone.now()
                    pending_rep.save()
                    failed += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f"Error creating replica on {pending_rep.target_node.name}: {str(e)}"
                ))
                pending_rep.attempts += 1
                pending_rep.last_attempt = timezone.now()
                pending_rep.save()
                failed += 1

        self.stdout.write(self.style.SUCCESS(
            f"Completed processing: {processed} successful, {failed} failed, {skipped} skipped"
        ))