#!/usr/bin/env bash
# Build a standalone venvfinder executable for the current OS with PyInstaller.
# Usage:  ./build.sh
# Output: dist/venvfinder (single binary; also usable with --cli from a
#         terminal) and, on macOS, dist/venvfinder.app for Finder.
#
# Must be run on the target OS -- PyInstaller does not cross-compile.
set -euo pipefail
cd "$(dirname "$0")"

# PyInstaller lives in the project's dev dependency group (see pyproject.toml);
# `uv run` makes sure .venv exists and is up to date before using it.
uv run --group dev pyinstaller \
    --noconfirm --clean \
    --onefile \
    --windowed \
    --name venvfinder \
    --paths src \
    src/venvfinder/__main__.py

echo
echo "Built: dist/venvfinder"
[ -d dist/venvfinder.app ] && echo "Built: dist/venvfinder.app"
