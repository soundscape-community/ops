#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing Soundscape Backup systemd service and timer..."

# Copy service and timer files to systemd directory
sudo cp "${SCRIPT_DIR}/soundscape-backup.service" /etc/systemd/system/
sudo cp "${SCRIPT_DIR}/soundscape-backup.timer" /etc/systemd/system/

# Reload systemd to recognize the new files
echo "Reloading systemd daemon..."
sudo systemctl daemon-reload

# Enable and start the timer
echo "Enabling and starting the backup timer..."
sudo systemctl enable soundscape-backup.timer
sudo systemctl start soundscape-backup.timer

# Show timer status
echo ""
echo "Installation complete! Timer status:"
sudo systemctl status soundscape-backup.timer --no-pager

echo ""
echo "To check when the next backup is scheduled, run:"
echo "  systemctl list-timers soundscape-backup.timer"
