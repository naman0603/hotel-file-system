import os
import hashlib
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, FileResponse, Http404
from django.core.files.storage import default_storage
from django.contrib import messages
from django.utils import timezone
from .models import StoredFile, FileChunk, FileNode
from .forms import FileUploadForm
from .utils import FileChunker
from django.http import JsonResponse, StreamingHttpResponse
from django.core.exceptions import ValidationError


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