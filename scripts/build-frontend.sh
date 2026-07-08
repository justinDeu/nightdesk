#!/usr/bin/env bash
# Build the nightdesk SPA (frontend/) for production.
#
# Installs node_modules if missing (or --ci forces a clean `npm ci`), then
# runs the Vite build. Output lands in frontend/dist, which
# nightdesk.api.app.create_app serves at the app root `/` when present (see
# nightdesk.api.spa; override the dist location with NIGHTDESK_SPA_DIST) — no
# dist dir means no mount and `/` just returns nothing useful, the JSON API
# under /api is unaffected either way. Re-run this script after any frontend
# change; there is no separate "watch and rebuild" mode for production
# output (use `npm run dev` in frontend/ for hot reload during development).

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
