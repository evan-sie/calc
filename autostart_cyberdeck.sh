#!/bin/bash

# This script is designed to be run by a systemd service.
# It ensures all components start in the correct order.

# Set a path for the logs this script will create
LOG_FILE="/tmp/autostart_cyberdeck.log"

echo "--- Cyberdeck Autostart Initialized ---" > $LOG_FILE

# Run the backend script (Sway, VNC, AI) in the background.
# We redirect its output to our log file.
/root/cyberdeck_boot.sh >> $LOG_FILE 2>&1 &

echo "Backend process launched. Waiting 10 seconds for stabilization..." >> $LOG_FILE
sleep 10

# Check if Sway actually started by looking for its socket file.
if [ -z "$(ls /run/user/0/sway-ipc.*.sock 2>/dev/null)" ]; then
    echo "CRITICAL ERROR: Sway socket not found after 10s. Backend failed." >> $LOG_FILE
    # We exit here, and systemd will restart the whole process.
    exit 1
fi

echo "Sway is active. Starting persistent bridge connection loop..." >> $LOG_FILE
while true; do
    # Log each connection attempt
    echo "Attempting to connect bridge..." >> $LOG_FILE
    /root/cg-virtual-monitor/vnc-client/build/cgvm_vnc --calc
    echo "Bridge disconnected. Retrying in 3 seconds..." >> $LOG_FILE
    sleep 3
done
