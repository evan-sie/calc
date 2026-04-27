#!/bin/bash

# --- 1. SET ENVIRONMENT ---
export XDG_RUNTIME_DIR=/run/user/$(id -u)
if [ ! -d "$XDG_RUNTIME_DIR" ]; then
    mkdir -p "$XDG_RUNTIME_DIR"
    chmod 0700 "$XDG_RUNTIME_DIR"
fi
export WLR_BACKENDS=headless
export WAYLAND_DISPLAY=wayland-1
export WLR_RENDERER=pixman

# --- NEW: Force Headless Output via Env Var (Fixes 0Hz issue) ---
# Format: Name:Width:Height:RefreshRate
# Using 396x224 - confirmed 384x216 had gray borders on right/bottom edges
export WLR_HEADLESS_OUTPUTS=1 

# --- 2. CLEANUP ---
pkill -9 foot
pkill -9 cgvm_vnc
pkill -9 wayvnc
pkill -9 sway
rm -f $XDG_RUNTIME_DIR/sway-ipc.*.sock
rm -f $XDG_RUNTIME_DIR/$WAYLAND_DISPLAY
sleep 1

# --- 3. START SWAY ---
echo "[+] Starting Sway..."
sway --config /root/.config/sway/config > /tmp/sway.log 2>&1 &
SWAY_PID=$!

# --- 4. FIND SOCKET ---
echo "[.] Waiting for Sway socket..."
TIMEOUT=0
while [ -z "$(ls $XDG_RUNTIME_DIR/sway-ipc.*.sock 2>/dev/null)" ]; do
    sleep 0.5
    ((TIMEOUT++))
    if [ $TIMEOUT -gt 20 ]; then
        echo "[!] Sway failed to start. Check /tmp/sway.log"
        exit 1
    fi
done
export SWAYSOCK=$(ls $XDG_RUNTIME_DIR/sway-ipc.*.sock | head -n 1)

# --- 5. SETUP SCREEN ---
# Note: We removed 'create_output' because WLR_HEADLESS_OUTPUTS handles it now.
# We just reinforce the resolution here.
swaymsg -s $SWAYSOCK output "HEADLESS-1" resolution 396x224@60Hz

# --- 6. START VNC ---
echo "[+] Starting VNC Server..."
wayvnc -C /root/.config/wayvnc/config -o "HEADLESS-1" 127.0.0.1 5900 > /tmp/wayvnc.log 2>&1 &

# --- 7. KEYBOARD FIX ---
sleep 2 
swaymsg -s $SWAYSOCK input "0:0:wlr_virtual_keyboard_v1" repeat_rate 0

echo "[SUCCESS] Cyberdeck Backend is Live!"
wait $SWAY_PID
