#!/usr/bin/env bash
# Build the nightdesk SPA (frontend/) for production.
#
# Installs node_modules if missing (or --ci forces a clean `npm ci`), then
# runs the Vite build. Output lands in frontend/dist, which
# nightdesk.api.app.create_app mounts at /app when present (see
# nightdesk.api.spa) — no dist dir means no mount, so running this script
# is entirely optional for HTMX-only / API-only use.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRONTEND_DIR="${REPO_ROOT}/frontend"
cd "${FRONTEND_DIR}"

if [[ "${1:-}" == "--ci" ]] || [[ ! -d node_modules ]]; then
  echo "Installing frontend dependencies..."
  npm ci
fi

echo "Building frontend/dist..."
npm run build

echo "Built $(find dist -type f | wc -l) files into ${FRONTEND_DIR}/dist."
