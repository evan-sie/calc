# DietPi Restore Notes

This repo is the file bundle for restoring the Casio FX-CG50 cyberdeck after a
fresh DietPi image. Install packages/toolchains separately, then copy these
runtime files into place.

## Important Files

- `casio_ai.py` -> `/root/casio_ai.py`
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
