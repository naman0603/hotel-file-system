#!/bin/bash
# setup_distributed.sh

echo "Setting up Distributed File Storage System"
echo "=========================================="

# Create directories for MinIO nodes
mkdir -p ~/minio-data/server1
mkdir -p ~/minio-data/server2
mkdir -p ~/minio-data/server3

# Install requirements
pip install -r requirements.txt

# Run database migrations
python manage.py makemigrations
python manage.py migrate

# Setup initial nodes
python manage.py manage_nodes add --name "Node1" --hostname "localhost" --port 9001 --console-port 9091 --primary
python manage.py manage_nodes add --name "Node2" --hostname "localhost" --port 9002 --console-port 9092 --priority 1
python manage.py manage_nodes add --name "Node3" --hostname "localhost" --port 9003 --console-port 9093 --priority 2
python manage.py manage_nodes add --name "Node4" --hostname "localhost" --port 9004 --console-port 9094 --priority 3
python manage.py manage_nodes add --name "Node5" --hostname "localhost" --port 9005 --console-port 9095 --priority 4


echo "=========================================="
echo "Setup complete! Start the MinIO servers with:"
echo "Terminal 1: minio server ~/minio-data/server1 --address :9001 --console-address :9091"
echo "Terminal 2: minio server ~/minio-data/server2 --address :9002 --console-address :9092"
echo "Terminal 3: minio server ~/minio-data/server3 --address :9003 --console-address :9093"
echo "Terminal 4: minio server ~/minio-data/server4 --address :9004 --console-address :9094"
echo "Terminal 5: minio server ~/minio-data/server5 --address :9005 --console-address :9095"
echo ""
echo "Then start the Django server:"
echo "python manage.py runserver"
echo ""
echo "Check node health with:"
echo "python manage.py manage_nodes health"