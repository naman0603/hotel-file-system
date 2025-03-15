from django.core.management.base import BaseCommand
from file_storage.models import FileNode
from file_storage.node_manager import NodeManager
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Manage distributed storage nodes'

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest='command', help='Command to run')

        # List nodes
        list_parser = subparsers.add_parser('list', help='List all nodes')

        # Add a new node
        add_parser = subparsers.add_parser('add', help='Add a new storage node')
        add_parser.add_argument('--name', type=str, required=True, help='Node name')
        add_parser.add_argument('--hostname', type=str, required=True, help='Node hostname')
        add_parser.add_argument('--port', type=int, required=True, help='Node port')
        add_parser.add_argument('--console-port', type=int, required=True, help='Node console port')
        add_parser.add_argument('--access-key', type=str, default='minioadmin', help='Access key')
        add_parser.add_argument('--secret-key', type=str, default='minioadmin', help='Secret key')
        add_parser.add_argument('--bucket', type=str, default='hotel-files', help='Bucket name')
        add_parser.add_argument('--priority', type=int, default=0, help='Node priority (lower is higher)')
        add_parser.add_argument('--primary', action='store_true', help='Set as primary node')

        # Change node status
        status_parser = subparsers.add_parser('status', help='Change node status')
        status_parser.add_argument('--node-id', type=int, required=True, help='Node ID')
        status_parser.add_argument('--status', type=str, required=True,
                                   choices=['active', 'inactive', 'maintenance'],
                                   help='New status')

        # Check nodes health
        health_parser = subparsers.add_parser('health', help='Check nodes health')

        # Elect primary node
        elect_parser = subparsers.add_parser('elect-primary', help='Elect primary node')

    def handle(self, *args, **options):
        command = options['command']

        if command == 'list':
            self.list_nodes()
        elif command == 'add':
            self.add_node(options)
        elif command == 'status':
            self.change_status(options)
        elif command == 'health':
            self.check_health()
        elif command == 'elect-primary':
            self.elect_primary()
        else:
            self.stderr.write("Invalid command")

    def list_nodes(self):
        """List all storage nodes"""
        nodes = FileNode.objects.all().order_by('priority')

        if not nodes:
            self.stdout.write("No storage nodes configured")
            return

        self.stdout.write("Storage Nodes:")
        self.stdout.write("=============")

        for node in nodes:
            primary = " (PRIMARY)" if node.is_primary else ""
            self.stdout.write(
                f"ID: {node.id} | Name: {node.name}{primary} | "
                f"Address: {node.hostname}:{node.port} | "
                f"Status: {node.status} | Priority: {node.priority}"
            )

    def add_node(self, options):
        """Add a new storage node"""
        try:
            node = FileNode.objects.create(
                name=options['name'],
                hostname=options['hostname'],
                port=options['port'],
                console_port=options['console_port'],
                access_key=options['access_key'],
                secret_key=options['secret_key'],
                bucket_name=options['bucket'],
                priority=options['priority'],
                is_primary=options['primary']
            )

            self.stdout.write(self.style.SUCCESS(f"Node '{node.name}' added successfully with ID {node.id}"))

            # Check if the node is reachable
            if NodeManager.check_node_availability(node):
                self.stdout.write(self.style.SUCCESS(f"Node '{node.name}' is reachable"))
            else:
                self.stdout.write(self.style.WARNING(f"Node '{node.name}' is not reachable"))

        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error adding node: {str(e)}"))

    def change_status(self, options):
        """Change a node's status"""
        try:
            node = FileNode.objects.get(id=options['node_id'])
            old_status = node.status
            node.status = options['status']
            node.save()

            self.stdout.write(self.style.SUCCESS(
                f"Node '{node.name}' status changed from '{old_status}' to '{node.status}'"
            ))

        except FileNode.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"Node with ID {options['node_id']} not found"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error changing node status: {str(e)}"))

    def check_health(self):
        """Check health of all nodes"""
        nodes = FileNode.objects.all()

        if not nodes:
            self.stdout.write("No storage nodes configured")
            return

        healthy = 0
        unhealthy = 0

        self.stdout.write("Node Health Check:")
        self.stdout.write("=================")

        for node in nodes:
            is_healthy = NodeManager.check_node_availability(node)
            status = self.style.SUCCESS("HEALTHY") if is_healthy else self.style.ERROR("UNHEALTHY")

            self.stdout.write(f"Node '{node.name}' ({node.hostname}:{node.port}): {status}")

            if is_healthy:
                healthy += 1
            else:
                unhealthy += 1

        self.stdout.write("=================")
        self.stdout.write(f"Summary: {healthy} healthy nodes, {unhealthy} unhealthy nodes")

    def elect_primary(self):
        """Elect a primary node"""
        primary = NodeManager.get_primary_node()

        if primary:
            self.stdout.write(self.style.SUCCESS(
                f"Elected node '{primary.name}' as primary node"
            ))
        else:
            self.stderr.write(self.style.ERROR("Failed to elect a primary node"))