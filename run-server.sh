#!/usr/bin/env bash
# Production entry point for PM2 (server_runner.py + scheduler).
# Validates venv before exec so a broken env is logged instead of a silent PM2 failure.

set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=scripts/ensure_venv.sh
source "$(dirname "$0")/scripts/ensure_venv.sh"

PYTHON="$(ensure_venv)" || exit 1
exec "$PYTHON" server_runner.py
