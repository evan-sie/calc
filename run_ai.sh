#!/bin/bash
# Keep foot open and recover from an unexpected Python crash. No network action.
cd /root || exit 1
while true; do
    python3 /root/casio_ai.py
    result=$?
    if [ "$result" -eq 0 ]; then
        exit 0
    fi
    printf '\nCalculator app exited (%s). Restarting in 3 seconds...\n' "$result"
    sleep 3
done
