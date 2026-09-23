#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-${ROOT_DIR}/dist}"
PYTHON_BIN="${PYTHON:-python3}"

"${ROOT_DIR}/scripts/build-ui.sh"

echo ">> Building sdist and wheel"
mkdir -p "${OUTPUT_DIR}"
(cd "${ROOT_DIR}" && "${PYTHON_BIN}" -m build --outdir "${OUTPUT_DIR}")

echo ">> Built artifacts in ${OUTPUT_DIR}"
