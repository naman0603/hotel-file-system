from django.core.management.base import BaseCommand
from file_storage.jobs import create_chunk_replicas, verify_chunk_integrity


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
            help='Verify chunk integrity'
        )

    def handle(self, *args, **options):
        if options['verify']:
            self.stdout.write('Verifying chunk integrity...')
            verify_chunk_integrity()
            self.stdout.write(self.style.SUCCESS('Integrity verification complete'))

        self.stdout.write(f'Creating chunk replicas (min: {options["replicas"]})...')
        create_chunk_replicas(min_replicas=options['replicas'])
        self.stdout.write(self.style.SUCCESS('Replica creation complete'))