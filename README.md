# Hotel File System

A distributed file storage and retrieval system for hotel management built with Django.

## Project Overview

This system allows hotel staff to upload and retrieve files efficiently. Files are stored in chunks across multiple nodes to ensure redundancy, scalability, and optimized retrieval.

## Technology Stack

- Backend: Django (Python-based web framework)
- Storage: MinIO
- Database: SQLite (development) / PostgreSQL (production)
- Version Control: Git & GitHub

## Setup Instructions

1. Clone the repository

git clone https://github.com/naman0603/hotel-file-system.git
cd hotel-file-system

2. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

3. Install dependencies
pip install -r requirements.txt


4. Configure environment variables
- Create a `.env` file in the project root
- Add required environment variables (see `.env.example`)

5. Run migrations
python manage.py migrate

6. Create superuser
python manage.py createsuperuser

7. Start MinIO server
minio server minio_data --console-address ":9001"

8. Run development server
python manage.py runserver


## Project Structure

- `distributed_storage/`: Django project settings
- `file_storage/`: Main application for file handling and storage

## Features

- File upload and metadata storage
- Distributed storage across nodes
- File retrieval optimization
- Redundancy and error handling
- Admin interface for monitoring