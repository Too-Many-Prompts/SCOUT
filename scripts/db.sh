#!/bin/bash
# Manage the test Oracle container: up | down | reset | logs
# Data lives in the scout-oradata volume: down keeps it, reset wipes it (and reseeds on next start).
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-}" in
  up)    docker compose up -d --wait ;;
  down)  docker compose down ;;  # keeps the data volume
  reset) docker compose down -v && docker compose up -d --wait ;;
  logs)  docker compose logs -f oracle ;;
  *)     echo "usage: $0 up|down|reset|logs" >&2; exit 2 ;;
esac
