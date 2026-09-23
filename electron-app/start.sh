#!/bin/bash
# Quick start script for Tempo Electron app

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHON="${ROOT_DIR}/.venv/bin/python"

echo "Using Python: $PYTHON"
echo "FastAPI check:"
$PYTHON -c "import fastapi; print('  ✓ FastAPI', fastapi.__version__)" || echo "  ✗ FastAPI not found!"

cd "$(dirname "$0")"
npm run dev
