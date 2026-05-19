#!/usr/bin/env bash
# Build the Tailwind CSS bundle for nightdesk.
#
# Downloads the Tailwind v4 standalone CLI into .tailwind/ if missing,
# then compiles src/styles/app.css -> src/nightdesk/static/app.css.
#
# Content sources are declared via `@source` inside src/styles/app.css,
# so Tailwind v4 scans templates automatically (the v4 CLI has no --content flag).

set -euo pipefail

TAILWIND_VERSION="${TAILWIND_VERSION:-v4.3.0}"
TAILWIND_DIR=".tailwind"
TAILWIND_BIN="${TAILWIND_DIR}/tailwindcss"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${REPO_ROOT}"

detect_asset() {
  local os arch
  os="$(uname -s)"
  arch="$(uname -m)"
  case "${os}" in
    Linux)
      case "${arch}" in
        x86_64|amd64) echo "tailwindcss-linux-x64" ;;
        aarch64|arm64) echo "tailwindcss-linux-arm64" ;;
        *) echo "unsupported-arch:${arch}" >&2; return 1 ;;
      esac
      ;;
    Darwin)
      case "${arch}" in
        x86_64) echo "tailwindcss-macos-x64" ;;
        arm64) echo "tailwindcss-macos-arm64" ;;
        *) echo "unsupported-arch:${arch}" >&2; return 1 ;;
      esac
      ;;
    *) echo "unsupported-os:${os}" >&2; return 1 ;;
  esac
}

if [[ ! -x "${TAILWIND_BIN}" ]]; then
  asset="$(detect_asset)"
  url="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}"
  echo "Downloading Tailwind CLI ${TAILWIND_VERSION} (${asset})..."
  mkdir -p "${TAILWIND_DIR}"
  curl -fSL --retry 3 -o "${TAILWIND_BIN}" "${url}"
  chmod +x "${TAILWIND_BIN}"
fi

echo "Building src/nightdesk/static/app.css..."
"${TAILWIND_BIN}" \
  -i src/styles/app.css \
  -o src/nightdesk/static/app.css \
  --minify

echo "Built $(wc -c < src/nightdesk/static/app.css) bytes."
