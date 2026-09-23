#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="${ROOT_DIR}/electron-app"
UI_DIR="${ROOT_DIR}/tempo/ui"
PNPM_BIN="${PNPM:-pnpm}"

echo ">> Installing the pinned frontend dependency graph"
(cd "${FRONTEND_DIR}" && "${PNPM_BIN}" install --frozen-lockfile)

echo ">> Checking both TypeScript targets"
(cd "${FRONTEND_DIR}" && "${PNPM_BIN}" typecheck)

echo ">> Building the browser UI"
(cd "${FRONTEND_DIR}" && "${PNPM_BIN}" build:python-ui)

test -f "${FRONTEND_DIR}/dist-python/index.html"
test -d "${FRONTEND_DIR}/dist-python/assets"
mkdir -p "${UI_DIR}"
rm -f "${UI_DIR}/index.html"
rm -rf "${UI_DIR}/assets"
cp "${FRONTEND_DIR}/dist-python/index.html" "${UI_DIR}/index.html"
cp -R "${FRONTEND_DIR}/dist-python/assets" "${UI_DIR}/assets"
