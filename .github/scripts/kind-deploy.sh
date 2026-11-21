#!/usr/bin/env bash
# CI's Kubernetes deploy, up to the point where the hub answers: waits for the
# kind cluster ci.yml started at the top of the job, loads the image CI built
# into it and applies deploy/kubernetes.yaml. ci.yml runs this in the
# background (bg.sh) while the Compose and Swarm steps run, then tests the hub.
set -euo pipefail

"$(dirname "$0")/bg.sh" wait kind-cluster 240
kind load docker-image exuvia:ci --name exuvia
kubectl create namespace exuvia
kubectl -n exuvia create secret generic exuvia --from-literal=api-token=ci-token
sed 's#ghcr.io/rlancer/exuvia:latest#exuvia:ci#' deploy/kubernetes.yaml | kubectl -n exuvia apply -f -
kubectl -n exuvia rollout status deployment/exuvia-hub --timeout=180s
