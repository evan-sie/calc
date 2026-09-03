# DietPi Restore Notes

This repo is the file bundle for restoring the Casio FX-CG50 cyberdeck after a
fresh DietPi image. Install packages/toolchains separately, then copy these
runtime files into place.

## Important Files

- `casio_ai.py` -> `/root/casio_ai.py`
- `setup_calc.sh` -> `/root/setup_calc.sh`
- `cyberdeck_boot.sh` -> `/root/cyberdeck_boot.sh`
- `restart_ai.sh` -> `/root/restart_ai.sh`
- `autostart_cyberdeck.sh` -> `/root/autostart_cyberdeck.sh`
- `config/sway/config` -> `/root/.config/sway/config`
- `config/wayvnc/config` -> `/root/.config/wayvnc/config`
- `systemd/cyberdeck.service` -> `/etc/systemd/system/cyberdeck.service`
- `systemd/wifi-ensure.service` -> `/etc/systemd/system/wifi-ensure.service`
- `cg-virtual-monitor/vnc-client` -> `/root/cg-virtual-monitor/vnc-client`

## Copy Files On The Pi

Run these commands from the cloned repo on the Raspberry Pi:

```bash
install -m 755 casio_ai.py /root/casio_ai.py
install -m 755 setup_calc.sh /root/setup_calc.sh
install -m 755 cyberdeck_boot.sh /root/cyberdeck_boot.sh
install -m 755 restart_ai.sh /root/restart_ai.sh
install -m 755 autostart_cyberdeck.sh /root/autostart_cyberdeck.sh

mkdir -p /root/.config/sway /root/.config/wayvnc
install -m 644 config/sway/config /root/.config/sway/config
install -m 644 config/wayvnc/config /root/.config/wayvnc/config

mkdir -p /root/cg-virtual-monitor
cp -a cg-virtual-monitor/vnc-client /root/cg-virtual-monitor/

install -m 644 systemd/cyberdeck.service /etc/systemd/system/cyberdeck.service
install -m 644 systemd/wifi-ensure.service /etc/systemd/system/wifi-ensure.service
systemctl daemon-reload
systemctl enable cyberdeck.service wifi-ensure.service
```

## API Keys

Keys are not in the source and never committed. `casio_ai.py` reads them from
`/root/.casio_ai.env`, which you must create by hand after a restore:

```bash
cat > /root/.casio_ai.env <<'EOF'
# API keys for casio_ai.py. Never commit this file.
GEMINI_API_KEY=your-gemini-key-here
OPENAI_API_KEY=your-openai-key-here
EOF
chmod 600 /root/.casio_ai.env
```

A real environment variable of the same name takes precedence over the file.
If a key is missing, the deck still starts and says so in the chat rather than
failing silently.

## Class Notes

The class-context feature reads markdown files from `/root/notes`, which is a
separate repo:

```bash
git clone https://github.com/evan-sie/casio-notes.git /root/notes
```

Without it the class selector has nothing to list.

## DietPi Settings To Reapply

The README log notes these settings were important on DietPi:

```bash
ln -sf /usr/share/zoneinfo/America/Chicago /etc/localtime
echo "America/Chicago" > /etc/timezone
```

For the OV5647 camera, keep the DietPi camera blacklists disabled and ensure
`/boot/config.txt` has enough GPU memory and the correct overlay:

```ini
gpu_mem_512=128
dtoverlay=vc4-kms-v3d
dtoverlay=ov5647
```

The Pi Zero 2 W only supports 2.4 GHz WiFi. Make sure the hotspot SSID is
broadcasting on 2.4 GHz.
