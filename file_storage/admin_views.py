from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from django.db.models import Count, Sum
from django.utils import timezone
from django.contrib import messages
from .models import FileNode, StoredFile, FileChunk, ChunkStatus
from .health import SystemHealth
from .redundancy import RedundancyManager
import json


@staff_member_required
def admin_dashboard(request):
    """Custom admin dashboard for system monitoring"""

    # Get overall system health
    system_status = SystemHealth.get_overall_status()

    # Node statistics
    nodes = FileNode.objects.all()
    total_nodes = nodes.count()
    active_nodes = nodes.filter(status='active').count()

    # Storage statistics
    total_files = StoredFile.objects.count()
    total_chunks = FileChunk.objects.count()
    original_chunks = FileChunk.objects.filter(is_replica=False).count()
    replica_chunks = FileChunk.objects.filter(is_replica=True).count()

    # Calculate total storage
    total_bytes = FileChunk.objects.aggregate(
        total=Sum('size_bytes')
    )['total'] or 0

    # Calculate health metrics
    corrupt_chunks = FileChunk.objects.filter(status=ChunkStatus.CORRUPT).count()
    failed_chunks = FileChunk.objects.filter(status=ChunkStatus.FAILED).count()

    # Get recent activity
    recent_files = StoredFile.objects.order_by('-upload_date')[:10]
    recent_issues = FileChunk.objects.filter(
        status__in=[ChunkStatus.CORRUPT, ChunkStatus.FAILED]
    ).select_related('file', 'node').order_by('-updated_at')[:10]

    # Get distribution of files by type
    file_types = StoredFile.objects.values('file_type').annotate(
        count=Count('id')
    ).order_by('-count')

    # Get node distribution
    node_distribution = FileChunk.objects.values('node__name').annotate(
        count=Count('id')
    ).order_by('-count')

    context = {
        'system_status': system_status,
        'node_stats': {
            'total': total_nodes,
            'active': active_nodes,
            'inactive': total_nodes - active_nodes,
        },
        'storage_stats': {
            'total_files': total_files,
            'total_chunks': total_chunks,
            'original_chunks': original_chunks,
            'replica_chunks': replica_chunks,
            'total_bytes': total_bytes,
            'corrupt_chunks': corrupt_chunks,
            'failed_chunks': failed_chunks,
        },
        'recent_files': recent_files,
        'recent_issues': recent_issues,
        'file_types': file_types,
        'node_distribution': node_distribution,
    }

    return render(request, 'admin/file_storage/dashboard.html', context)


@staff_member_required
def admin_node_management(request):
    """Node management interface for administrators"""
    nodes = FileNode.objects.all().order_by('name')

    # Process node actions
    if request.method == 'POST':
        action = request.POST.get('action')
        node_id = request.POST.get('node_id')

        if node_id and action:
            node = get_object_or_404(FileNode, id=node_id)

            if action == 'activate':
                node.status = 'active'
                node.save()
                messages.success(request, f"Node '{node.name}' activated successfully.")
            elif action == 'deactivate':
                node.status = 'inactive'
                node.save()
                messages.success(request, f"Node '{node.name}' deactivated successfully.")
            elif action == 'maintenance':
                node.status = 'maintenance'
                node.save()
                messages.success(request, f"Node '{node.name}' set to maintenance mode.")
            elif action == 'delete':
                # Check if node has chunks
                if node.stored_chunks.count() > 0:
                    messages.error(
                        request,
                        f"Cannot delete node '{node.name}' as it contains {node.stored_chunks.count()} chunks. "
                        f"Migrate chunks to another node first."
                    )
                else:
                    node_name = node.name
                    node.delete()
                    messages.success(request, f"Node '{node_name}' deleted successfully.")

        return redirect('admin:node_management')

    # Get node health information
    for node in nodes:
        node.health_info = SystemHealth.get_node_health(node)

    context = {
        'nodes': nodes,
        'title': 'Node Management',
    }

    return render(request, 'admin/file_storage/node_management.html', context)


@staff_member_required
def admin_storage_report(request):
    """Storage usage and distribution reports"""

    # Get overall storage stats
    total_storage = FileChunk.objects.aggregate(total=Sum('size_bytes'))['total'] or 0

    # Get user storage distribution
    user_storage = StoredFile.objects.values(
        'uploader__username'
    ).annotate(
        file_count=Count('id'),
        total_size=Sum('size_bytes')
    ).order_by('-total_size')

    # Get file type distribution
    file_type_distribution = StoredFile.objects.values(
        'file_type'
    ).annotate(
        file_count=Count('id'),
        total_size=Sum('size_bytes')
    ).order_by('-total_size')

    # Get node distribution
    node_storage = FileChunk.objects.values(
        'node__name'
    ).annotate(
        chunk_count=Count('id'),
        total_size=Sum('size_bytes')
    ).order_by('-total_size')

    # Get historical data (in a real system, this would be from database logs)
    # For demo, we'll create some placeholder data
    now = timezone.now()
    historical_data = []
    for i in range(7):
        date = now - timezone.timedelta(days=i)
        historical_data.append({
            'date': date.strftime('%Y-%m-%d'),
            'storage_used': max(0, total_storage * (0.9 - (i * 0.05))),  # Simulated historical data
            'file_count': max(0, StoredFile.objects.count() * (0.9 - (i * 0.05))),
        })

    context = {
        'total_storage': total_storage,
        'user_storage': user_storage,
        'file_type_distribution': file_type_distribution,
        'node_storage': node_storage,
        'historical_data': historical_data,
        'title': 'Storage Reports',
    }

    return render(request, 'admin/file_storage/storage_report.html', context)


@staff_member_required
def admin_system_maintenance(request):
    """System maintenance controls"""

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'verify_integrity':
            # Run integrity verification
            from django.core.management import call_command
            try:
                call_command('maintain_storage', verify=True)
                messages.success(request, "Integrity verification started successfully.")
            except Exception as e:
                messages.error(request, f"Error starting integrity verification: {str(e)}")

        elif action == 'ensure_replicas':
            # Ensure replica count
            replicas = int(request.POST.get('replica_count', 1))
            try:
                redundancy_manager = RedundancyManager(min_replicas=replicas)
                stats = redundancy_manager.ensure_minimum_replicas()
                messages.success(
                    request,
                    f"Replica creation complete: {stats['created']} replicas created, "
                    f"{stats['failed']} failed, {stats['already_sufficient']} already sufficient."
                )
            except Exception as e:
                messages.error(request, f"Error ensuring replicas: {str(e)}")

        elif action == 'rebalance_nodes':
            # In a real system, this would rebalance chunks across nodes
            messages.info(request, "Node rebalancing is not implemented in this demo version.")

        return redirect('admin:system_maintenance')

    # Get maintenance stats
    total_chunks = FileChunk.objects.count()
    original_chunks = FileChunk.objects.filter(is_replica=False).count()
    replica_chunks = FileChunk.objects.filter(is_replica=True).count()

    no_replica_chunks = FileChunk.objects.filter(
        is_replica=False
    ).exclude(
        chunk_number__in=FileChunk.objects.filter(
            is_replica=True
        ).values('file', 'chunk_number')
    ).count()

    context = {
        'title': 'System Maintenance',
        'maintenance_stats': {
            'total_chunks': total_chunks,
            'original_chunks': original_chunks,
            'replica_chunks': replica_chunks,
            'no_replica_chunks': no_replica_chunks,
            'replica_ratio': round(replica_chunks / original_chunks, 2) if original_chunks else 0,
        }
    }

    return render(request, 'admin/file_storage/system_maintenance.html', context)


@staff_member_required
def ajax_node_status(request):
    """AJAX endpoint to get real-time node status"""
    nodes = FileNode.objects.all()
    node_data = []

    for node in nodes:
        health = SystemHealth.get_node_health(node)
        node_data.append({
            'id': node.id,
            'name': node.name,
            'status': node.status,
            'health_status': health['health_status'],
            'chunks': {
                'total': health['chunks']['total'],
                'corrupt': health['chunks']['corrupt'],
                'failed': health['chunks']['failed'],
                'health_percentage': health['chunks']['health_percentage'],
            }
        })

    return JsonResponse({
        'nodes': node_data,
        'timestamp': timezone.now().isoformat(),
    })