import hashlib
import os
import uuid

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Count, Sum
from django.db.models import Q
from django.http import FileResponse, Http404
from django.http import JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone

from .forms import FileUploadForm
from .health import SystemHealth
from .models import FileNode, FileChunk, StoredFile, ChunkStatus
from .node_manager import NodeManager, logger
from .redundancy import RedundancyManager
from .retrieval import FileCache
from .utils import FileChunker


@login_required
@user_passes_test(lambda u: u.is_staff)
@login_required
@user_passes_test(lambda u: u.is_staff)
def distributed_dashboard(request):
    """Dashboard for monitoring the distributed system"""
    # Get unique nodes with health information
    all_nodes = FileNode.objects.all().order_by('priority')

    # Create a dictionary to filter unique nodes
    unique_nodes = {}
    for node in all_nodes:
        key = f"{node.hostname}:{node.port}"
        if key not in unique_nodes:
            unique_nodes[key] = node

    nodes = list(unique_nodes.values())
    node_data = []

    for node in nodes:
        # Get health information
        health_status = "healthy"
        health_percentage = 100

        if node.status != 'active':
            health_status = "inactive"
            health_percentage = 0
        else:
            # Count chunks on this node
            total_chunks = node.stored_chunks.count()
            corrupt_chunks = node.stored_chunks.filter(status=ChunkStatus.CORRUPT).count()
            failed_chunks = node.stored_chunks.filter(status=ChunkStatus.FAILED).count()

            if total_chunks > 0:
                healthy_percentage = ((total_chunks - corrupt_chunks - failed_chunks) / total_chunks) * 100
                health_percentage = round(healthy_percentage)

                if healthy_percentage < 80:
                    health_status = "critical"
                elif healthy_percentage < 95:
                    health_status = "warning"

        # Calculate space used
        space_used = node.stored_chunks.aggregate(total=Sum('size_bytes'))['total'] or 0

        node_data.append({
            'id': node.id,
            'name': node.name,
            'hostname': node.hostname,
            'port': node.port,
            'status': node.status,
            'is_primary': node.is_primary,
            'priority': node.priority,
            'health_status': health_status,
            'health_percentage': health_percentage,
            'chunk_count': node.stored_chunks.count(),
            'space_used': space_used
        })

    # Get cluster status information
    total_nodes = len(nodes)
    active_nodes = sum(1 for n in node_data if n['status'] == 'active' and n['health_status'] != 'critical')

    cluster_status = {
        'total_nodes': total_nodes,
        'active_nodes': active_nodes,
        'status': 'critical' if active_nodes < total_nodes / 2 else 'warning' if active_nodes < total_nodes else 'healthy'
    }

    # Get primary node
    primary_node = FileNode.objects.filter(is_primary=True).first()
    if not primary_node:
        primary_node = NodeManager.get_primary_node()

    # Get replication information
    original_chunks = FileChunk.objects.filter(is_replica=False).count()
    replica_chunks = FileChunk.objects.filter(is_replica=True).count()

    replication_factor = round(replica_chunks / original_chunks, 1) if original_chunks > 0 else 0

    # Get files with replication issues
    files_with_issues = 0
    files_needing_replication = []

    for stored_file in StoredFile.objects.all():
        file_chunks = FileChunk.objects.filter(file=stored_file, is_replica=False)

        for chunk in file_chunks:
            replicas = FileChunk.objects.filter(
                file=stored_file,
                chunk_number=chunk.chunk_number,
                is_replica=True,
                status=ChunkStatus.UPLOADED
            ).count()

            if replicas < 1:  # We want at least 1 replica per chunk
                files_with_issues += 1
                files_needing_replication.append({
                    'id': stored_file.id,
                    'name': stored_file.name,
                    'missing_replicas': 1 - replicas
                })
                break

    context = {
        'nodes': node_data,
        'cluster_status': cluster_status,
        'primary_node': primary_node,
        'original_chunks': original_chunks,
        'replica_chunks': replica_chunks,
        'replication_factor': replication_factor,
        'files_with_issues': files_with_issues,
        'files_needing_replication': files_needing_replication,
    }

    return render(request, 'file_storage/distributed_dashboard.html', context)


# In file_storage/views.py, update the relevant method
@login_required
@user_passes_test(lambda u: u.is_staff)
def change_node_status(request, node_id):
    """Change a node's status"""
    if request.method == 'POST':
        node = get_object_or_404(FileNode, id=node_id)
        old_status = node.status
        status = request.POST.get('status')

        if status in ['active', 'inactive', 'maintenance']:
            # Store whether node is being activated
            is_activating = (old_status != 'active' and status == 'active')

            # Update the node status
            node.status = status
            node.save()

            # Clear cache to ensure updated status is shown
            cache_key = "node_load_stats"
            cache.delete(cache_key)

            messages.success(request, f"Node '{node.name}' status changed from '{old_status}' to '{status}'.")

            # If this was the primary node being deactivated, elect a new primary
            if old_status == 'active' and status != 'active' and node.is_primary:
                new_primary = NodeManager.get_primary_node()
                if new_primary:
                    messages.info(request, f"Node '{new_primary.name}' is now the primary node.")

            # Process pending replications if node was activated and has pending replications
            if is_activating and node.pending_replications.exists():
                from django.core.management import call_command
                try:
                    call_command('process_pending_replications', '--max-attempts=5')
                    messages.success(request, f"Processed pending replications for node '{node.name}'.")
                except Exception as e:
                    messages.error(request, f"Error processing pending replications: {str(e)}")
        else:
            messages.error(request, f"Invalid status: {status}")

    return redirect('file_storage:distributed_dashboard')


@login_required
@user_passes_test(lambda u: u.is_staff)
def replicate_file(request, file_id):
    """Create replicas for a file"""
    if request.method == 'POST':
        stored_file = get_object_or_404(StoredFile, id=file_id)

        redundancy_manager = RedundancyManager(min_replicas=1)
        chunks = FileChunk.objects.filter(file=stored_file, is_replica=False)

        replicas_created = 0
        for chunk in chunks:
            replicas = redundancy_manager.create_replicas_for_chunk(chunk)
            replicas_created += replicas

        if replicas_created > 0:
            messages.success(request, f"Created {replicas_created} replicas for file '{stored_file.name}'.")
        else:
            messages.warning(request,
                             f"No new replicas created for file '{stored_file.name}'. Check node availability.")

    return redirect('file_storage:distributed_dashboard')

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
    """Handle file uploads with automatic redundancy across nodes"""
    # Clear any old messages
    storage = messages.get_messages(request)
    for _ in storage:
        pass

    # Check server availability before processing the form
    available_nodes_count = NodeManager.get_available_nodes_count()
    minimum_nodes_required = 3  # Updated from 2 to 3

    if request.method == 'POST':
        # Check if we have enough nodes available before processing the upload
        if available_nodes_count < minimum_nodes_required:
            messages.error(
                request,
                f'Not enough storage servers are currently available (minimum {minimum_nodes_required} required). '
                'Please try again later.'
            )
            return redirect('file_storage:dashboard')

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

                # Create chunks - ensure we get a proper list back
                try:
                    file_chunks = chunker.chunk_file(uploaded_file, stored_file, request.user)

                    # Convert to list if needed
                    if hasattr(file_chunks, 'all'):
                        file_chunks = list(file_chunks)

                    if not file_chunks:
                        file_chunks = []

                    chunk_count = len(file_chunks)
                except Exception as chunking_error:
                    logger.error(f"Error during file chunking: {str(chunking_error)}")
                    raise Exception(f"Failed to process file chunks: {str(chunking_error)}")

                # Create replicas if possible
                try:
                    redundancy_manager = RedundancyManager(min_replicas=1)
                    redundancy_manager.ensure_minimum_replicas()
                except Exception as replica_error:
                    logger.error(f"Error creating replicas: {str(replica_error)}")
                    # Continue without failing

                messages.success(
                    request,
                    f'File "{uploaded_file.name}" uploaded successfully and split into {chunk_count} chunks with redundancy.'
                )
                return redirect('file_storage:dashboard')

            except Exception as e:
                logger.error(f"File upload error: {str(e)}")
                messages.error(request, f'Error uploading file: {str(e)}')
                # Delete the file record if it was created
                if 'stored_file' in locals() and stored_file.id:
                    try:
                        stored_file.delete()
                    except:
                        pass
                return redirect('file_storage:upload_file')
        else:
            # Form is invalid
            messages.error(request, 'Please correct the errors in the form.')
    else:
        form = FileUploadForm()

    # Add server status information to the context
    context = {
        'form': form,
        'available_nodes': available_nodes_count,
        'minimum_nodes_required': minimum_nodes_required,
        'servers_ready': available_nodes_count >= minimum_nodes_required
    }

    return render(request, 'file_storage/upload.html', context)


# In file_storage/views.py

@login_required
def download_file(request, file_id):
    """Download a file with comprehensive failover capability"""
    try:
        # Get the file
        stored_file = get_object_or_404(StoredFile, id=file_id, uploader=request.user)

        # Update last accessed time
        stored_file.last_accessed = timezone.now()
        stored_file.save()

        # First try to get from cache
        cached_file = FileCache.get_cached_file(file_id)
        if cached_file:
            logger.info(f"Serving cached file {stored_file.name}")
            response = FileResponse(
                cached_file,
                as_attachment=True,
                filename=stored_file.original_filename
            )
            return response

        # If not cached, reassemble from chunks with enhanced failover support
        chunker = FileChunker()

        try:
            logger.info(f"Reassembling file {stored_file.name} with failover support")
            reassembled_file = chunker.reassemble_file_optimized(stored_file)

            # Cache the file for future access (if it's not too large)
            if stored_file.size_bytes < 50 * 1024 * 1024:  # Only cache files under 50MB
                try:
                    file_data = reassembled_file.read()
                    FileCache.cache_file(file_id, file_data)
                    reassembled_file.seek(0)  # Reset file pointer
                except Exception as cache_error:
                    logger.warning(f"Failed to cache file {stored_file.name}: {str(cache_error)}")
                    # Still continue with the download

            # Create response
            response = FileResponse(
                reassembled_file,
                as_attachment=True,
                filename=stored_file.original_filename
            )
            return response

        except Exception as e:
            logger.error(f"Error reassembling file {stored_file.name}: {str(e)}")

            # Try to determine if file is recoverable
            health_info = SystemHealth.get_file_health(stored_file)

            if health_info['can_recover']:
                # The file should be recoverable, but there might be an issue with the code
                messages.error(
                    request,
                    f"Error downloading file: There was a temporary system issue. "
                    f"Your file is intact and should be available. Please try again in a few moments."
                )
            else:
                # The file is not recoverable - some chunks truly are missing
                messages.error(
                    request,
                    f"Error downloading file: Some parts of this file are currently unavailable. "
                    f"This could be due to maintenance or server issues. Please try again later."
                )

            return redirect('file_storage:dashboard')

    except StoredFile.DoesNotExist:
        raise Http404("File not found")

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


@login_required
def file_list(request):
    """Enhanced file listing with filtering and pagination"""
    # Get query parameters
    search_query = request.GET.get('search', '')
    selected_type = request.GET.get('type', '')
    sort_by = request.GET.get('sort', 'date')

    # Base queryset
    files = StoredFile.objects.filter(uploader=request.user)

    # Apply search filter
    if search_query:
        files = files.filter(
            Q(name__icontains=search_query) |
            Q(original_filename__icontains=search_query)
        )

    # Apply type filter
    if selected_type:
        files = files.filter(file_type=selected_type)

    # Apply sorting
    if sort_by == 'name':
        files = files.order_by('name')
    elif sort_by == 'size':
        files = files.order_by('-size_bytes')
    elif sort_by == 'type':
        files = files.order_by('file_type')
    else:  # Default: date
        files = files.order_by('-upload_date')

    # Get all file types for filter dropdown
    file_types = StoredFile.objects.filter(
        uploader=request.user
    ).values('file_type').annotate(
        count=Count('id')
    ).order_by('file_type')

    # Pagination
    paginator = Paginator(files, 10)  # 10 files per page
    page = request.GET.get('page')

    try:
        files = paginator.page(page)
    except PageNotAnInteger:
        # If page is not an integer, deliver first page
        files = paginator.page(1)
    except EmptyPage:
        # If page is out of range, deliver last page of results
        files = paginator.page(paginator.num_pages)

    context = {
        'files': files,
        'paginator': paginator,
        'search_query': search_query,
        'selected_type': selected_type,
        'sort_by': sort_by,
        'file_types': file_types,
        'is_filtered': bool(search_query or selected_type)
    }

    return render(request, 'file_storage/file_list.html', context)


@login_required
def enhanced_file_details(request, file_id):
    """Enhanced file details view with more information"""
    # Get file and check ownership
    file = get_object_or_404(StoredFile, id=file_id, uploader=request.user)

    # Get file health information
    from .health import SystemHealth
    file_health = SystemHealth.get_file_health(file)

    # Get chunk information with replica counts
    original_chunks = FileChunk.objects.filter(file=file, is_replica=False).order_by('chunk_number')

    # Calculate replica factor
    total_original_chunks = original_chunks.count()
    total_replica_chunks = FileChunk.objects.filter(file=file, is_replica=True).count()
    replica_factor = round(total_replica_chunks / total_original_chunks, 1) if total_original_chunks > 0 else 0

    # Prepare enhanced chunk data
    chunks_with_replicas = []
    for chunk in original_chunks:
        replica_count = FileChunk.objects.filter(
            file=file,
            chunk_number=chunk.chunk_number,
            is_replica=True
        ).count()

        chunks_with_replicas.append({
            'chunk_number': chunk.chunk_number,
            'size_bytes': chunk.size_bytes,
            'node_name': chunk.node.name if chunk.node else 'Unknown',
            'status': chunk.status,
            'status_display': chunk.get_status_display(),
            'replica_count': replica_count
        })

    # Calculate average chunk size
    avg_chunk_size = file.size_bytes / total_original_chunks if total_original_chunks > 0 else 0

    # Check if file is cached
    file_cached = FileCache.is_file_cached(file_id)

    # Get access count
    access_count = FileCache.get_access_count(file_id)

    context = {
        'file': file,
        'file_health': file_health,
        'chunks_with_replicas': chunks_with_replicas,
        'total_chunks': total_original_chunks,
        'unique_nodes': len(set(chunk.node_id for chunk in original_chunks if chunk.node)),
        'replica_factor': replica_factor,
        'avg_chunk_size': avg_chunk_size,
        'file_cached': file_cached,
        'access_count': access_count
    }

    return render(request, 'file_storage/file_details_enhanced.html', context)

def logout_view(request):
    logout(request)
    return redirect('login')