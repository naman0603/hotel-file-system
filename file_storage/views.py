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


@login_required
def dashboard(request):
    """Main dashboard view for file storage system"""
    files = StoredFile.objects.filter(uploader=request.user).order_by('-upload_date')
    nodes = FileNode.objects.all()

    context = {
        'files': files,
        'nodes': nodes,
        'total_files': files.count(),
        'total_size': sum(f.size_bytes for f in files) if files else 0,
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
    """Handle file uploads"""
    if request.method == 'POST':
        form = FileUploadForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded_file = request.FILES['file']

            # Generate file checksum
            file_hash = hashlib.sha256()
            for chunk in uploaded_file.chunks():
                file_hash.update(chunk)
            checksum = file_hash.hexdigest()

            # Reset file pointer
            uploaded_file.seek(0)

            # Save file to storage
            file_path = default_storage.save(
                f'uploads/{request.user.username}/{uploaded_file.name}',
                uploaded_file
            )

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

            # Create a single chunk for now (will be chunked properly in Phase 2)
            node = FileNode.objects.filter(status='active').first()

            if node:
                chunk = FileChunk(
                    file=stored_file,
                    chunk_number=1,
                    size_bytes=uploaded_file.size,
                    checksum=checksum,
                    storage_path=file_path,
                    node=node,
                    is_replica=False
                )
                chunk.save()

                messages.success(request, f'File "{uploaded_file.name}" uploaded successfully.')
                return redirect('file_storage:dashboard')
            else:
                messages.error(request, 'No active storage nodes available.')
        else:
            messages.error(request, 'Error uploading file. Please try again.')
    else:
        form = FileUploadForm()

    return render(request, 'file_storage/upload.html', {'form': form})


@login_required
def download_file(request, file_id):
    """Download a file"""
    try:
        # Get the file
        stored_file = StoredFile.objects.get(id=file_id, uploader=request.user)

        # Update last accessed time
        stored_file.last_accessed = timezone.now()
        stored_file.save()

        # For now, just get the first chunk (Phase 1)
        # In Phase 2, we'll implement proper chunk assembly
        chunk = stored_file.chunks.first()

        if not chunk:
            raise Http404("File chunks not found")

        # Get the file from storage
        file_path = chunk.storage_path
        file = default_storage.open(file_path)

        # Create response
        response = FileResponse(file, as_attachment=True, filename=stored_file.original_filename)
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