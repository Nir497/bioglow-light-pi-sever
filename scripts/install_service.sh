#!/usr/bin/env bash
#
# Install BioGlow as a systemd service that starts automatically on boot.
#
# Usage:
#   sudo ./scripts/install_service.sh [options]
#
# Options:
#   --user <username>       User account to run the service (defaults to sudo user)
#   --port <port>           Dashboard HTTP port (defaults to 8000)
#   --no-animal-analysis    Run without the local Perch animal identification model
#   --dry-run               Print the generated systemd service unit without installing
#   -h, --help              Show this help message
#

set -euo pipefail

# Determine script and repository root directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVICE_NAME="bioglow.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"

# Defaults
PORT=8000
DRY_RUN=false
EXTRA_ARGS=""
TARGET_USER=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      TARGET_USER="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --no-animal-analysis)
      EXTRA_ARGS="${EXTRA_ARGS} --no-animal-analysis"
      shift
      ;;
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

# Resolve target user if not specified
if [ -z "${TARGET_USER}" ]; then
  if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    TARGET_USER="${SUDO_USER}"
  else
    TARGET_USER="$(id -un)"
  fi
fi

# Verify user exists
if ! id "${TARGET_USER}" >/dev/null 2>&1; then
  echo "Error: User '${TARGET_USER}' does not exist on this system." >&2
  exit 1
fi

TARGET_GROUP="$(id -gn "${TARGET_USER}")"

# Determine Python path
VENV_PYTHON="${REPO_DIR}/.venv/bin/python"
if [ ! -x "${VENV_PYTHON}" ]; then
  echo "Warning: Virtual environment python not found at ${VENV_PYTHON}." >&2
  if command -v python3 >/dev/null 2>&1; then
    VENV_PYTHON="$(command -v python3)"
    echo "Falling back to system python3: ${VENV_PYTHON}" >&2
  else
    echo "Error: No Python executable found. Please create the virtual environment first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
fi

# Construct ExecStart command
EXEC_CMD="${VENV_PYTHON} ${REPO_DIR}/birdcast_service.py --port ${PORT}${EXTRA_ARGS}"

# Generate systemd unit content
UNIT_CONTENT="[Unit]
Description=BioGlow Light & Sensor Dashboard Service
After=network.target network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=${TARGET_USER}
Group=${TARGET_GROUP}
WorkingDirectory=${REPO_DIR}
ExecStart=${EXEC_CMD}
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"

# Handle dry run
if [ "${DRY_RUN}" = true ]; then
  echo "=== Generated ${SERVICE_NAME} (${UNIT_PATH}) ==="
  echo "${UNIT_CONTENT}"
  exit 0
fi

# Require root for installation
if [ "${EUID}" -ne 0 ]; then
  echo "Error: Root privileges are required to install systemd services." >&2
  echo "Please run: sudo $0 $*" >&2
  exit 1
fi

echo "Installing BioGlow service for user '${TARGET_USER}'..."

# Ensure target user belongs to necessary hardware groups if they exist
for grp in dialout i2c gpio audio; do
  if getent group "${grp}" >/dev/null 2>&1; then
    if ! id -nG "${TARGET_USER}" | grep -qw "${grp}"; then
      echo "Adding user '${TARGET_USER}' to group '${grp}' for hardware access..."
      usermod -aG "${grp}" "${TARGET_USER}" || true
    fi
  fi
done

# Write service file
echo "Writing unit file to ${UNIT_PATH}..."
printf "%s" "${UNIT_CONTENT}" > "${UNIT_PATH}"
chmod 0644 "${UNIT_PATH}"

# Reload systemd
echo "Reloading systemd daemon..."
systemctl daemon-reload

# Enable service to run at boot
echo "Enabling ${SERVICE_NAME} on boot..."
systemctl enable "${SERVICE_NAME}"

# Stop any manually running birdcast_service.py instances to prevent port 8000 collisions
PIDS=$(pgrep -f "birdcast_service.py" || true)
if [ -n "${PIDS}" ]; then
  echo "Stopping manually running birdcast_service.py process(es): ${PIDS}..."
  kill ${PIDS} 2>/dev/null || true
  sleep 1
fi

# Start/restart service
echo "Starting ${SERVICE_NAME}..."
systemctl restart "${SERVICE_NAME}"

# Wait briefly and report status
sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}"; then
  echo "✓ ${SERVICE_NAME} is active and running!"
  echo ""
  echo "Dashboard URL: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '127.0.0.1'):${PORT}/"
  echo ""
  echo "Useful commands:"
  echo "  Check status:  sudo systemctl status ${SERVICE_NAME}"
  echo "  View live log: journalctl -u ${SERVICE_NAME} -f"
  echo "  Restart:       sudo systemctl restart ${SERVICE_NAME}"
  echo "  Stop:          sudo systemctl stop ${SERVICE_NAME}"
  echo "  Uninstall:     sudo ${SCRIPT_DIR}/uninstall_service.sh"
else
  echo "⚠ Warning: ${SERVICE_NAME} failed to start or exited immediately." >&2
  echo "Check logs with: journalctl -u ${SERVICE_NAME} -n 30 --no-pager" >&2
  exit 1
fi
