#!/usr/bin/env bash
# Runs the existing CityCare API + Government Portal analytics + the prototype on one port:
#   http://localhost:8000/portal/   (prototype-v2)
#   http://localhost:8000/docs      (API docs)
# Set PORTAL_DIR if prototype-v2 lives somewhere else; add --fresh to reload the demo data.
set -e
cd "$(dirname "$0")/.."
export PORTAL_DIR="${PORTAL_DIR:-$HOME/Downloads/Civic-Connect/civicconnect-repo/prototype-v2}"
if [ "$1" = "--fresh" ]; then
  python3 -c "from app.analytics.store import get_repo; get_repo().reset(); print('demo data reset')"
fi
exec python3 -m uvicorn app.analytics_app:app --host 127.0.0.1 --port "${PORT:-8000}"
