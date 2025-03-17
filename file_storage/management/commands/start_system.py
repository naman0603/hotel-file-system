# file_storage/management/commands/start_system.py
from django.core.management.base import BaseCommand
import subprocess
import os
import sys
import time


class Command(BaseCommand):
    help = 'Start the complete system including background monitors'

    def handle(self, *args, **options):
        self.stdout.write("Starting Distributed File Storage System")

        # Start the node monitor in the background
        monitor_process = subprocess.Popen(
            [sys.executable, 'manage.py', 'monitor_nodes', '--interval=30'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        self.stdout.write("Node monitor started in background")
        self.stdout.write("Starting Django development server...")

        # Start the Django server (this will block until terminated)
        os.system('python manage.py runserver')

        # When Django server stops, also stop the monitor
        if monitor_process.poll() is None:  # If still running
            monitor_process.terminate()
            self.stdout.write("Node monitor stopped")