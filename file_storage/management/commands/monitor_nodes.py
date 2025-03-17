# file_storage/management/commands/monitor_nodes.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.core.management import call_command
import time
import logging
from file_storage.models import FileNode, PendingReplication
from file_storage.node_manager import NodeManager

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Monitor nodes and process pending replications when nodes come back online'

    def add_arguments(self, parser):
        parser.add_argument(
            '--interval',
            type=int,
            default=60,
            help='Check interval in seconds'
        )

    def handle(self, *args, **options):
        interval = options['interval']
        self.stdout.write(f"Starting node monitor with {interval} second interval")

        # Track nodes that were previously offline
        offline_nodes = set()

        try:
            while True:
                # Get nodes with pending replications
                nodes_with_pending = set(
                    FileNode.objects.filter(
                        pending_replications__isnull=False
                    ).values_list('id', flat=True)
                )

                # If there are no pending replications, nothing to monitor
                if not nodes_with_pending:
                    self.stdout.write("No pending replications to process")
                    time.sleep(interval)
                    continue

                # Check each node with pending replications
                newly_online_nodes = []

                for node_id in nodes_with_pending:
                    try:
                        node = FileNode.objects.get(id=node_id)

                        # Skip if node is marked inactive in database
                        if node.status != 'active':
                            continue

                        # Check if node is now available
                        is_available = NodeManager.check_node_availability(node)

                        # If node was offline but is now online, process its pending replications
                        if is_available and node_id in offline_nodes:
                            self.stdout.write(f"Node {node.name} is now online. Processing its pending replications...")
                            newly_online_nodes.append(node.name)
                            offline_nodes.remove(node_id)

                        # Track offline nodes
                        if not is_available and node_id not in offline_nodes:
                            offline_nodes.add(node_id)
                            self.stdout.write(
                                f"Node {node.name} is offline. Will monitor for when it comes back online.")

                    except FileNode.DoesNotExist:
                        continue

                # If any nodes came online, process their pending replications
                if newly_online_nodes:
                    self.stdout.write(f"Processing pending replications for nodes: {', '.join(newly_online_nodes)}")
                    call_command('process_pending_replications')

                # Wait for the next check
                time.sleep(interval)

        except KeyboardInterrupt:
            self.stdout.write("Node monitor stopped")