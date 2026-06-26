#!/usr/bin/env bash
# Validates the project virtualenv before PM2 starts server_runner.py.
# Logs to stderr (PM2 error log) and logs/pm2-startup.log.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_FILE="${PROJECT_ROOT}/logs/pm2-startup.log"

log_msg() {
  local level="$1"
  shift
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S') ${level} $*"
  echo "$line" >&2
  mkdir -p "$(dirname "$LOG_FILE")"
  echo "$line" >> "$LOG_FILE"
}

find_python() {
  if [ -x "${PROJECT_ROOT}/venv/bin/python" ]; then
    echo "${PROJECT_ROOT}/venv/bin/python"
    return 0
  fi
  if [ -x "${PROJECT_ROOT}/.venv/bin/python" ]; then
    echo "${PROJECT_ROOT}/.venv/bin/python"
    return 0
  fi
  if [ -x "${PROJECT_ROOT}/.venv/Scripts/python.exe" ]; then
    echo "${PROJECT_ROOT}/.venv/Scripts/python.exe"
    return 0
  fi
  return 1
}

log_broken_venv() {
  log_msg "ERROR" "PM2 startup failed: virtualenv Python not found."
  log_msg "ERROR" "  Expected: ${PROJECT_ROOT}/venv/bin/python"
  log_msg "ERROR" "  Or:       ${PROJECT_ROOT}/.venv/bin/python"

  if [ -d "${PROJECT_ROOT}/venv" ]; then
    log_msg "ERROR" "  ${PROJECT_ROOT}/venv exists but bin/python is missing or not executable."
    log_msg "ERROR" "  Fix: cd ${PROJECT_ROOT} && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
  elif [ -d "${PROJECT_ROOT}/.venv" ]; then
    log_msg "ERROR" "  ${PROJECT_ROOT}/.venv exists but bin/python is missing or not executable."
    log_msg "ERROR" "  Fix: cd ${PROJECT_ROOT} && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
  else
    log_msg "ERROR" "  No venv directory found."
    log_msg "ERROR" "  Fix: cd ${PROJECT_ROOT} && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
  fi
}

ensure_venv() {
  local python_bin

  if ! python_bin="$(find_python)"; then
    log_broken_venv
    return 1
  fi

  if ! "$python_bin" -c "import uvicorn, fastapi" >/dev/null 2>&1; then
    log_msg "ERROR" "PM2 startup failed: ${python_bin} exists but uvicorn/fastapi are not installed."
    log_msg "ERROR" "  Fix: ${python_bin} -m pip install -r ${PROJECT_ROOT}/requirements.txt"
    return 1
  fi

  log_msg "INFO" "venv ok: ${python_bin}"
  echo "$python_bin"
  return 0
}
