#/bin/bash
docker build -t boing7898/rtmgr-sim:latest .
podman tag localhost/boing7898/rtmgr-sim:latest docker.io/boing7898/rtmgr-sim:latest
docker push boing7898/rtmgr-sim:latest
