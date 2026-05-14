#!/usr/bin/env bash
set -euo pipefail

# Install or update the OSTI hourly sync cron job in /etc/cron.d/osti-sync
# This script must be run with appropriate permissions (sudo).

CRON_FILE="/etc/cron.d/osti-sync"
SCRIPT_PATH="/opt/osti/bin/osti_hourly_sync.sh"
LOGS_DIR="/opt/osti/logs"
LOG_FILE="$LOGS_DIR/cron.log"

# Validate that the script exists and is executable
if [[ ! -f "$SCRIPT_PATH" ]]; then
  echo "ERROR: Script not found at $SCRIPT_PATH"
  exit 1
fi

if [[ ! -x "$SCRIPT_PATH" ]]; then
  echo "WARNING: $SCRIPT_PATH is not executable. Making it executable."
  chmod +x "$SCRIPT_PATH"
fi

# Ensure log directory exists
if [[ ! -d "$LOGS_DIR" ]]; then
  echo "Creating log directory: $LOGS_DIR"
  mkdir -p "$LOGS_DIR"
fi

# Create or update cron file
# Format: minute hour day month dow user command
CRON_CONTENT="# OSTI Hourly Sync Job
# Syncs CBI records from OSTI E-Link API hourly and publishes to web

SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

# Run sync at the top of every hour (minute 0)
0 * * * * root $SCRIPT_PATH >> $LOG_FILE 2>&1
"

# Check if we have sudo (this script likely needs to be run as root)
if [[ $EUID -ne 0 ]]; then
  echo "ERROR: This script must be run as root (use: sudo $0)"
  exit 1
fi

echo "Installing OSTI hourly sync cron job..."
echo "$CRON_CONTENT" > "$CRON_FILE"
chmod 644 "$CRON_FILE"

# Verify syntax
if ! crontab -l -u root 2>/dev/null | grep -q osti-sync; then
  # This is just a basic check; actual verification happens when cron daemon reads the file
  :
fi

# Try to validate the cron file if possible
if command -v cron-validate >/dev/null 2>&1; then
  cron-validate "$CRON_FILE" || {
    echo "WARNING: Cron syntax validation failed"
    exit 1
  }
fi

echo "SUCCESS: Cron job installed at $CRON_FILE"
echo "Log file will be written to: $LOG_FILE"
echo ""
echo "To verify installation, run:"
echo "  sudo crontab -l"
echo ""
echo "To view recent logs, run:"
echo "  tail -f $LOG_FILE"
