from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required, user_passes_test
from .models import FileNode, FileChunk, ChunkStatus, StoredFile
from .health import SystemHealth
import json
import uuid


@login_required
def get_node_status(request):
    """API endpoint to get status of all nodes"""
    nodes = FileNode.objects.all()

    nodes_data = []
    for node in nodes:
        chunks = node.stored_chunks.count()
        nodes_data.append({
            'id': node.id,
            'name': node.name,
            'hostname': node.hostname,
            'port': node.port,
            'status': node.status,
            'chunks': chunks,
            'created_at': node.created_at.isoformat(),
            'updated_at': node.updated_at.isoformat(),
        })

    return JsonResponse({
        'nodes': nodes_data,
        'total_nodes': len(nodes_data),
        'active_nodes': sum(1 for node in nodes_data if node['status'] == 'active'),
    })


@csrf_exempt
@require_http_methods(["POST"])
def update_node_status(request, node_id):
    """API endpoint to update a node's status"""
    try:
        node = FileNode.objects.get(id=node_id)
        data = json.loads(request.body)

        if 'status' in data:
            node.status = data['status']
            node.save()

            return JsonResponse({
                'success': True,
                'message': f"Node {node.name} status updated to {node.status}"
            })
        else:
            return JsonResponse({
                'success': False,
                'message': "Status field is required"
            }, status=400)

    except FileNode.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': "Node not found"
        }, status=404)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'message': str(e)
        }, status=500)


@login_required
def system_health(request):
    """API endpoint to get overall system health"""
    health_status = SystemHealth.get_overall_status()
    return JsonResponse(health_status)


@login_required
def node_health(request, node_id):
    """API endpoint to get health status of a specific node"""
    try:
        node = FileNode.objects.get(id=node_id)
        health_status = SystemHealth.get_node_health(node)
        return JsonResponse(health_status)
    except FileNode.DoesNotExist:
        return JsonResponse({'error': 'Node not found'}, status=404)


@login_required
def file_health(request, file_id):
    """API endpoint to get health status of a specific file"""
    try:
        file_uuid = uuid.UUID(file_id)
        stored_file = StoredFile.objects.get(id=file_uuid, uploader=request.user)
        health_status = SystemHealth.get_file_health(stored_file)
        return JsonResponse(health_status)
    except (ValueError, StoredFile.DoesNotExist):
        return JsonResponse({'error': 'File not found'}, status=404)


@login_required
def all_nodes_health(request):
    """API endpoint to get health status of all nodes"""
    nodes = FileNode.objects.all()
    nodes_health = [SystemHealth.get_node_health(node) for node in nodes]

    return JsonResponse({
        'nodes': nodes_health,
        'total': len(nodes_health)
    })


@login_required
def user_files_health(request):
    """API endpoint to get health status of all files owned by user"""
    files = StoredFile.objects.filter(uploader=request.user)
    files_health = [SystemHealth.get_file_health(f) for f in files]

    # Calculate overall statistics
    total_files = len(files_health)
    healthy_files = sum(1 for f in files_health if f['health_status'] == 'healthy')
    warning_files = sum(1 for f in files_health if f['health_status'] == 'warning')
    critical_files = sum(1 for f in files_health if f['health_status'] == 'critical')

    return JsonResponse({
        'files': files_health,
        'stats': {
            'total': total_files,
            'healthy': healthy_files,
            'warning': warning_files,
            'critical': critical_files
        }
    })


# Admin-only health check
def is_admin(user):
    return user.is_staff or user.is_superuser


@user_passes_test(is_admin)
def admin_system_health(request):
    """Comprehensive health check for admin users"""
    # Overall status
    overall_status = SystemHealth.get_overall_status()

    # Node details
    nodes = FileNode.objects.all()
    nodes_health = [SystemHealth.get_node_health(node) for node in nodes]

    # File stats
    total_files = StoredFile.objects.count()

    # Files with issues
    files_with_issues = []
    for stored_file in StoredFile.objects.all():
        health = SystemHealth.get_file_health(stored_file)
        if health['health_status'] != 'healthy':
            files_with_issues.append(health)

    return JsonResponse({
        'overall': overall_status,
        'nodes': nodes_health,
        'files': {
            'total': total_files,
            'with_issues': files_with_issues,
            'issues_count': len(files_with_issues)
        }
    })