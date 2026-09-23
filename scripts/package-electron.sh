#!/bin/bash

# Package Electron app for distribution

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ELECTRON_DIR="$PROJECT_ROOT/electron-app"

echo "Packaging Electron app..."

# Rebuild the Python service and its packaged browser UI first.
"$PROJECT_ROOT/scripts/build-python-server.sh"

cd "$ELECTRON_DIR"

# Build React app first
echo "Building React app..."
pnpm run build

# Package with electron-builder
echo "Packaging with electron-builder..."
pnpm run build:electron

echo "Done! Check electron-app/dist/ for the packaged app."
