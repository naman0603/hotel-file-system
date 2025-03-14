import os
import hashlib
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, FileResponse, Http404
from django.core.files.storage import default_storage
from django.contrib import messages
from django.utils import timezone
from .models import StoredFile, FileChunk, FileNode, ChunkStatus
from .forms import FileUploadForm
from .utils import FileChunker
from django.http import JsonResponse, StreamingHttpResponse
from django.core.exceptions import ValidationError
# Add these imports to your existing views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import Http404
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from .health import SystemHealth
from .redundancy import RedundancyManager
import hashlib
import uuid


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