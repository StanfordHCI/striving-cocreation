#!/bin/bash

# Build Python server executable using PyInstaller

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$PROJECT_ROOT/dist/tempo-server"
RESOURCES_DIR="$PROJECT_ROOT/electron-app/resources/python-server"

echo "Building Python server executable..."

# The bundled server owns the production UI. Always rebuild it from the pinned
# frontend graph before PyInstaller collects the tempo package.
"$PROJECT_ROOT/scripts/build-ui.sh"

# Create build directory
mkdir -p "$BUILD_DIR"

# Check if PyInstaller is installed
if ! command -v pyinstaller &> /dev/null; then
    echo "PyInstaller not found. Installing..."
    pip install pyinstaller
fi

# Build with PyInstaller
cd "$PROJECT_ROOT"

pyinstaller --onefile \
  --name tempo-server \
  --distpath "$BUILD_DIR" \
  --workpath "$PROJECT_ROOT/build" \
  --specpath "$PROJECT_ROOT/build" \
  --add-data "tempo:tempo" \
  --hidden-import tempo.server \
  --hidden-import tempo.system \
  --hidden-import tempo.pipelines.orchestrator \
  --hidden-import tempo.db \
  --hidden-import tempo.store \
  --hidden-import tempo.queries \
  --hidden-import tempo.observers.screen \
  --hidden-import tempo.os_backends \
  --hidden-import uvicorn \
  --hidden-import fastapi \
  --hidden-import websockets \
  tempo/server.py

# Create resources directory
mkdir -p "$RESOURCES_DIR"

# Copy executable to resources
if [ -f "$BUILD_DIR/tempo-server" ]; then
    cp "$BUILD_DIR/tempo-server" "$RESOURCES_DIR/"
    echo "✓ Python server built successfully: $RESOURCES_DIR/tempo-server"
elif [ -f "$BUILD_DIR/tempo-server.exe" ]; then
    cp "$BUILD_DIR/tempo-server.exe" "$RESOURCES_DIR/"
    echo "✓ Python server built successfully: $RESOURCES_DIR/tempo-server.exe"
else
    echo "✗ Error: Executable not found in $BUILD_DIR"
    exit 1
fi

echo "Done!"
