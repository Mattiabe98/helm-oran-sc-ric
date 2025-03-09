#/bin/bash
docker build -t boing7898/ric-plt-xapp-frame-py:latest .
podman tag localhost/boing7898/ric-plt-xapp-frame-py:latest docker.io/boing7898/ric-plt-xapp-frame-py:latest
docker push boing7898/ric-plt-xapp-frame-py:latest
