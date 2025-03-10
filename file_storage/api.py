from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from .models import FileNode, FileChunk, ChunkStatus
import json


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