import hashlib
import os
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import Count
from django.http import FileResponse, Http404
from django.http import JsonResponse
# Add these imports to your existing views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from .forms import FileUploadForm
from .health import SystemHealth
from .models import StoredFile, FileChunk, FileNode, ChunkStatus
from .redundancy import RedundancyManager
from .retrieval import FileCache
from .utils import FileChunker


# Add these new views to your views.py file
@login_required
def health_dashboard(request):
    """View for monitoring system health"""
    # Get overall system health
    system_status = SystemHealth.get_overall_status()

    # Get node health
    nodes = FileNode.objects.all()
    node_health = [SystemHealth.get_node_health(node) for node in nodes]

    # Get user files with health issues
    files = StoredFile.objects.filter(uploader=request.user)
    files_health = [SystemHealth.get_file_health(f) for f in files]

    # Filter out None values before trying to access their keys
    files_health = [f for f in files_health if f is not None]
    files_with_issues = [f for f in files_health if f['health_status'] != 'healthy']

    context = {
        'system_status': system_status,
        'nodes': node_health,
        'files_with_issues': files_with_issues,
    }

    return render(request, 'file_storage/health_dashboard.html', context)

@login_required
def repair_file(request, file_id):
    """Attempt to repair a file with issues"""
    try:
        file_uuid = uuid.UUID(file_id)
        stored_file = get_object_or_404(StoredFile, id=file_uuid, uploader=request.user)

        # Check file health
        health = SystemHealth.get_file_health(stored_file)

        if health['health_status'] == 'healthy':
            messages.info(request, f'File "{stored_file.name}" is already healthy.')
            return redirect('file_storage:file_details', file_id=file_id)

        # If file is not healthy, attempt repair
        redundancy_manager = RedundancyManager()

        # Repair all chunks of this file
        repaired_chunks = 0
        failed_chunks = 0

        # Process corrupt and failed chunks
        corrupt_chunks = FileChunk.objects.filter(
            file=stored_file,
            is_replica=False,
            status__in=[ChunkStatus.CORRUPT, ChunkStatus.FAILED]
        )

        for chunk in corrupt_chunks:
            if redundancy_manager.repair_chunk(chunk):
                repaired_chunks += 1
            else:
                failed_chunks += 1

        # Check for missing chunks
        chunks = stored_file.chunks.filter(is_replica=False)
        chunk_numbers = [chunk.chunk_number for chunk in chunks]
        expected_numbers = list(range(1, max(chunk_numbers) + 1)) if chunk_numbers else []
        missing_chunks = set(expected_numbers) - set(chunk_numbers)

        # Try to recover missing chunks from replicas
        missing_repaired = 0
        missing_failed = 0

        for chunk_num in missing_chunks:
            # Find a replica for this chunk
            replica = FileChunk.objects.filter(
                file=stored_file,
                chunk_number=chunk_num,
                is_replica=True,
                status=ChunkStatus.UPLOADED
            ).first()

            if replica:
                try:
                    # Read the replica
                    with default_storage.open(replica.storage_path, 'rb') as f:
                        chunk_data = f.read()

                    # Verify replica integrity
                    replica_hash = hashlib.sha256(chunk_data).hexdigest()

                    if replica_hash == replica.checksum:
                        # Create a new primary chunk from the replica
                        chunk_filename = f"{stored_file.id}_{chunk_num}_{uuid.uuid4().hex}.chunk"
                        storage_path = f"chunks/{stored_file.uploader.username}/{chunk_filename}"

                        default_storage.save(storage_path, ContentFile(chunk_data))

                        # Create new chunk record
                        FileChunk.objects.create(
                            file=stored_file,
                            chunk_number=chunk_num,
                            size_bytes=replica.size_bytes,
                            checksum=replica.checksum,
                            storage_path=storage_path,
                            node=replica.node,
                            is_replica=False,
                            status=ChunkStatus.UPLOADED
                        )

                        missing_repaired += 1
                    else:
                        missing_failed += 1
                except Exception as e:
                    missing_failed += 1
            else:
                missing_failed += 1

        # Create additional replicas if needed
        redundancy_manager.min_replicas = 2  # Ensure we have at least 2 replicas
        redundancy_manager.ensure_minimum_replicas()

        # Check final health status
        final_health = SystemHealth.get_file_health(stored_file)

        if final_health['health_status'] == 'healthy':
            messages.success(
                request,
                f'File "{stored_file.name}" successfully repaired! '
                f'Repaired {repaired_chunks + missing_repaired} chunks.'
            )
        elif final_health['health_status'] == 'warning':
            messages.warning(
                request,
                f'File "{stored_file.name}" partially repaired. '
                f'Repaired {repaired_chunks + missing_repaired} chunks, but {failed_chunks + missing_failed} chunks still have issues. '
                f'The file is recoverable but some chunks are using replicas.'
            )
        else:
            messages.error(
                request,
                f'Failed to fully repair file "{stored_file.name}". '
                f'Repaired {repaired_chunks + missing_repaired} chunks, but {failed_chunks + missing_failed} chunks could not be repaired. '
                f'The file may not be recoverable.'
            )

        return redirect('file_storage:file_details', file_id=file_id)

    except (ValueError, StoredFile.DoesNotExist):
        raise Http404("File not found")
    except Exception as e:
        messages.error(request, f"Error repairing file: {str(e)}")
        return redirect('file_storage:dashboard')

@login_required
def dashboard(request):
    """Main dashboard view for file storage system"""
    files = StoredFile.objects.filter(uploader=request.user).order_by('-upload_date')
    nodes = FileNode.objects.all()

    # Count all chunks (including replicas)
    total_chunks = FileChunk.objects.filter(file__uploader=request.user).count()

    # Count original chunks (non-replicas)
    original_chunks = FileChunk.objects.filter(
        file__uploader=request.user,
        is_replica=False
    ).count()

    # Count replicas
    replica_chunks = FileChunk.objects.filter(
        file__uploader=request.user,
        is_replica=True
    ).count()

    context = {
        'files': files,
        'nodes': nodes,
        'total_files': files.count(),
        'total_size': sum(f.size_bytes for f in files) if files else 0,
        'total_chunks': total_chunks,
        'original_chunks': original_chunks,
        'replica_chunks': replica_chunks,
    }

    return render(request, 'file_storage/dashboard.html', context)

def system_status(request):
    """API endpoint to check system status"""
    nodes = FileNode.objects.all()
    active_nodes = nodes.filter(status='active').count()

    return JsonResponse({
        'status': 'operational' if active_nodes > 0 else 'degraded',
        'total_nodes': nodes.count(),
        'active_nodes': active_nodes,
    })


@login_required
def upload_file(request):
    """Handle file uploads with chunking"""
    if request.method == 'POST':
        form = FileUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                uploaded_file = request.FILES['file']

                # Generate file checksum
                file_hash = hashlib.sha256()
                for chunk in uploaded_file.chunks():
                    file_hash.update(chunk)
                checksum = file_hash.hexdigest()

                # Reset file pointer
                uploaded_file.seek(0)

                # Create file record
                stored_file = StoredFile(
                    name=form.cleaned_data.get('description') or uploaded_file.name,
                    original_filename=uploaded_file.name,
                    file_type=os.path.splitext(uploaded_file.name)[1],
                    size_bytes=uploaded_file.size,
                    content_type=uploaded_file.content_type,
                    checksum=checksum,
                    uploader=request.user
                )
                stored_file.save()

                # Chunk and store the file
                chunker = FileChunker()
                chunks = chunker.chunk_file(uploaded_file, stored_file, request.user)

                messages.success(
                    request,
                    f'File "{uploaded_file.name}" uploaded successfully and split into {len(chunks)} chunks.'
                )
                return redirect('file_storage:dashboard')

            except Exception as e:
                messages.error(request, f'Error uploading file: {str(e)}')
        else:
            messages.error(request, 'Form validation failed. Please check the form.')
    else:
        form = FileUploadForm()

    return render(request, 'file_storage/upload.html', {'form': form})


# Update the download_file view
@login_required
def download_file(request, file_id):
    """Download a file by reassembling chunks"""
    try:
        # Get the file
        stored_file = StoredFile.objects.get(id=file_id, uploader=request.user)

        # Update last accessed time
        stored_file.last_accessed = timezone.now()
        stored_file.save()

        # Reassemble the file from chunks
        chunker = FileChunker()
        reassembled_file = chunker.reassemble_file(stored_file)

        # Create response
        response = FileResponse(
            reassembled_file,
            as_attachment=True,
            filename=stored_file.original_filename
        )
        return response

    except StoredFile.DoesNotExist:
        raise Http404("File not found")
    except Exception as e:
        messages.error(request, f"Error downloading file: {str(e)}")
        return redirect('file_storage:dashboard')

@login_required
def file_details(request, file_id):
    """Display detailed information about a file"""
    try:
        stored_file = StoredFile.objects.get(id=file_id, uploader=request.user)
        chunks = stored_file.chunks.all().order_by('chunk_number')

        context = {
            'file': stored_file,
            'chunks': chunks,
            'total_chunks': chunks.count(),
            'unique_nodes': len(set(chunk.node_id for chunk in chunks if chunk.node)),
        }

        return render(request, 'file_storage/file_details.html', context)

    except StoredFile.DoesNotExist:
        raise Http404("File not found")


@login_required
def download_file(request, file_id):
    """Download a file using optimized retrieval"""
    try:
        # Get the file
        stored_file = StoredFile.objects.get(id=file_id, uploader=request.user)

        # Update last accessed time
        stored_file.last_accessed = timezone.now()
        stored_file.save()

        # Check cache first
        cached_file = FileCache.get_cached_file(file_id)
        if cached_file:
            response = FileResponse(
                cached_file,
                as_attachment=True,
                filename=stored_file.original_filename
            )
            return response

        # If not cached, reassemble from chunks
        # Using enhanced chunker with NodeSelector
        chunker = FileChunker()
        reassembled_file = chunker.reassemble_file_optimized(stored_file)

        # Cache the file for future retrievals
        FileCache.cache_file(file_id, reassembled_file.read())
        reassembled_file.seek(0)  # Reset file pointer after reading

        # Create response
        response = FileResponse(
            reassembled_file,
            as_attachment=True,
            filename=stored_file.original_filename
        )
        return response

    except StoredFile.DoesNotExist:
        raise Http404("File not found")
    except Exception as e:
        messages.error(request, f"Error downloading file: {str(e)}")
        return redirect('file_storage:dashboard')


@login_required
def analytics_dashboard(request):
    """Dashboard for viewing file access analytics"""
    # Get user's files
    files = StoredFile.objects.filter(uploader=request.user).order_by('-last_accessed')

    # Get access statistics (in a real implementation, these would come from a database)
    file_stats = []
    for file in files:
        # Get cache status
        is_cached = FileCache.is_file_cached(file.id)
        access_count = FileCache.get_access_count(file.id)

        # Get node distribution information
        node_distribution = (
            FileChunk.objects.filter(file=file, is_replica=False)
            .values('node__name')
            .annotate(count=Count('id'))
            .order_by('-count')
        )

        # Calculate size distribution
        total_size = file.size_bytes
        chunk_count = FileChunk.objects.filter(file=file, is_replica=False).count()
        avg_chunk_size = total_size / chunk_count if chunk_count else 0

        file_stats.append({
            'file': file,
            'is_cached': is_cached,
            'access_count': access_count,
            'last_accessed': file.last_accessed,
            'node_distribution': node_distribution,
            'chunk_count': chunk_count,
            'avg_chunk_size': avg_chunk_size
        })

    context = {
        'file_stats': file_stats,
        'total_files': len(file_stats),
        'cached_files': sum(1 for stat in file_stats if stat['is_cached'])
    }

    return render(request, 'file_storage/analytics_dashboard.html', context)


@login_required
def cache_file(request, file_id):
    """Cache a specific file for faster access"""
    try:
        stored_file = get_object_or_404(StoredFile, id=file_id, uploader=request.user)

        # Check if already cached
        if FileCache.is_file_cached(file_id):
            messages.info(request, f'File "{stored_file.name}" is already cached.')
            return redirect('file_storage:analytics_dashboard')

        # Reassemble and cache
        chunker = FileChunker()
        reassembled_file = chunker.reassemble_file_optimized(stored_file)

        FileCache.cache_file(file_id, reassembled_file.read())

        messages.success(request, f'File "{stored_file.name}" has been cached for faster access.')

    except Exception as e:
        messages.error(request, f"Error caching file: {str(e)}")

    return redirect('file_storage:analytics_dashboard')