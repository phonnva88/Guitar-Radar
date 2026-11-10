#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
python_bin=.venv/bin/python
compose() { docker compose --env-file .env.phase1 -f compose.phase1.yml "$@"; }
case "${1:-help}" in
  setup)
    if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
      echo 'Install/start Docker Desktop with WSL integration, or Docker Engine with Compose v2, then rerun setup.' >&2
      exit 1
    fi
    # Rebuild even when an old binary exists: git pull may have updated the
    # collector's media extraction code since the previous setup.
    bash scripts/setup.sh
    "$python_bin" -m radar init-config
    compose up -d --wait
    "$python_bin" -m radar bootstrap
    echo 'Ready. Open .env.phase1 locally for RADAR_USERNAME and RADAR_PASSWORD. Start: bash scripts/phase1.sh start'
    ;;
  start)
    [[ -x "$python_bin" && -f .env.phase1 ]] || { echo 'Run bash scripts/phase1.sh setup first.' >&2; exit 1; }
    echo 'Guitar Radar: http://localhost:8000 (unless RADAR_PORT was changed). Ctrl+C stops the app.'
    exec "$python_bin" -m radar serve
    ;;
  stop-db) compose stop db ;;
  backup)
    mkdir -p data/backups
    backup_path="data/backups/phase1-$(date -u +%Y%m%dT%H%M%SZ).sql"
    compose exec -T db pg_dump -U radar -d guitar_radar > "$backup_path"
    echo "Backup saved: $backup_path"
    ;;
  *) echo 'Usage: bash scripts/phase1.sh {setup|start|stop-db|backup}' ;;
esac
