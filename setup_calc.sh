#!/bin/bash
set -e

echo "Step 2: Install Python packages"
# We use --break-system-packages because DietPi is Debian-based and we are running as root.
# Alternatively, we could create a venv, but the user said "Prefer apt packages where available... but make Python imports work".
# We already installed python3-pil, python3-opencv, python3-numpy via apt.
# google-genai is not in apt, so we use pip.
pip3 install --break-system-packages google-genai

echo "Step 3: Clone repo"
rm -rf /root/calc
git clone https://github.com/evan-sie/calc.git /root/calc

echo "Step 4: Copy files"
cp /root/calc/casio_ai.py /root/casio_ai.py
cp /root/calc/cyberdeck_boot.sh /root/cyberdeck_boot.sh
cp /root/calc/restart_ai.sh /root/restart_ai.sh
cp /root/calc/autostart_cyberdeck.sh /root/autostart_cyberdeck.sh

mkdir -p /root/.config/sway
cp /root/calc/config/sway/config /root/.config/sway/config

mkdir -p /root/.config/wayvnc
cp /root/calc/config/wayvnc/config /root/.config/wayvnc/config

cp /root/calc/systemd/cyberdeck.service /etc/systemd/system/cyberdeck.service
cp /root/calc/systemd/wifi-ensure.service /etc/systemd/system/wifi-ensure.service

mkdir -p /root/cg-virtual-monitor
cp -r /root/calc/cg-virtual-monitor/vnc-client /root/cg-virtual-monitor/

echo "Step 5: Make scripts executable"
chmod +x /root/casio_ai.py /root/cyberdeck_boot.sh /root/restart_ai.sh /root/autostart_cyberdeck.sh

echo "Step 8: Build the VNC bridge"
cd /root/cg-virtual-monitor/vnc-client
mkdir -p build
cd build
cmake ..
make -j2 || {
    echo "Build failed, attempting to build fxSDK/libfxlink..."
    cd /root
    rm -rf Fx-SDK
    git clone https://github.com/lephe/Fx-SDK.git
    cd Fx-SDK
    cmake -B build -DCMAKE_INSTALL_PREFIX=/usr/local -DFXLINK_DISABLE_UDISKS2=1
    make -C build -j2
    make -C build install
    
    echo "Retrying VNC bridge build..."
    cd /root/cg-virtual-monitor/vnc-client/build
    cmake ..
    make -j2
}

echo "Step 9: Reload and enable services"
systemctl daemon-reload
systemctl enable cyberdeck.service
systemctl enable wifi-ensure.service

echo "Step 10: Start service"
systemctl restart cyberdeck.service

echo "Setup complete."
