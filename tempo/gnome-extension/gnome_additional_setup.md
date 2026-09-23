# GNOME Extension Setup

This directory contains the GNOME Shell extension for Tempo.

## Installation

1. Copy the `you-be-anything@yba` directory to `~/.local/share/gnome-shell/extensions/`
2. Restart GNOME Shell (press Alt+F2, type 'r', press Enter)
3. Enable the extension using GNOME Extensions app or `gnome-extensions enable you-be-anything@yba`

## Configuration

The extension communicates with Tempo via D-Bus. Set the following environment variables:

- `GNOME_SESSION_BUS_NAME`: D-Bus service name (default: `org.yba.GnomeSession`)
- `GNOME_SESSION_OBJECT_PATH`: D-Bus object path (default: `/org/yba/GnomeSession`)

## Features

- Get global pointer position
- Send desktop notifications

## Requirements

- GNOME Shell 45+
- D-Bus access


