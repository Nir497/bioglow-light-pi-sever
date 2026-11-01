#!/usr/bin/env bash
#
# Uninstall the BioGlow systemd boot service.
#
# Usage:
#   sudo ./scripts/uninstall_service.sh [options]
#
# Options:
#   --dry-run               Print actions without modifying systemd
#   -h, --help              Show this help message
#

set -euo pipefail

SERVICE_NAME="bioglow.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Run '$0 --help' for usage." >&2
      exit 1
      ;;
  esac
done

if [ "${DRY_RUN}" = true ]; then
  echo "Dry run actions that would be performed:"
  echo "  1. systemctl stop ${SERVICE_NAME}"
  echo "  2. systemctl disable ${SERVICE_NAME}"
  echo "  3. rm -f ${UNIT_PATH}"
  echo "  4. systemctl daemon-reload"
  echo "  5. systemctl reset-failed"
  exit 0
fi

if [ "${EUID}" -ne 0 ]; then
  echo "Error: Root privileges are required to uninstall systemd services." >&2
  echo "Please run: sudo $0 $*" >&2
  exit 1
fi

echo "Stopping ${SERVICE_NAME}..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

echo "Disabling ${SERVICE_NAME}..."
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true

if [ -f "${UNIT_PATH}" ]; then
  echo "Removing unit file ${UNIT_PATH}..."
  rm -f "${UNIT_PATH}"
fi

echo "Reloading systemd daemon..."
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

echo "✓ ${SERVICE_NAME} has been uninstalled."
