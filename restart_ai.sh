#!/bin/bash
# High-Priority Cyberdeck UI Reset

# 1. Kill all potential camera and UI hogs
pkill -9 foot
pkill -9 ffplay
pkill -9 mpv
pkill -9 rpicam-vid
pkill -9 rpicam-jpeg
fuser -k -9 /dev/video0 2>/dev/null
sleep 0.5  # Allow camera to fully release

# 2. Re-establish Sway Environment
export SWAYSOCK=$(ls /run/user/$(id -u)/sway-ipc.*.sock 2>/dev/null | head -n 1)
export WAYLAND_DISPLAY=wayland-1
export XDG_RUNTIME_DIR=/run/user/0

# 3. Reload Sway config (picks up viewfinder window rules)
swaymsg reload 2>/dev/null

# 4. FIX KEYBOARD INPUT (Crucial Step)
# Forces Sway to re-bind the virtual keyboard from the VNC bridge
swaymsg input "0:0:wlr_virtual_keyboard_v1" repeat_rate 0 2>/dev/null

# 5. Relaunch UI
swaymsg exec "foot -a foot -e python3 /root/casio_ai.py"

echo "Cyberdeck Interface Re-initialized."
