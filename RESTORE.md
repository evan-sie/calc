# DietPi Restore Notes

This repo is the file bundle for restoring the Casio FX-CG50 cyberdeck after a
fresh DietPi image. Install packages/toolchains separately, then copy these
runtime files into place.

## Important Files

- `casio_ai.py` -> `/root/casio_ai.py`
- `wifi_manager.py` -> `/root/wifi_manager.py`
- `run_ai.sh` -> `/root/run_ai.sh`
- `setup_calc.sh` -> `/root/setup_calc.sh`
- `cyberdeck_boot.sh` -> `/root/cyberdeck_boot.sh`
- `restart_ai.sh` -> `/root/restart_ai.sh`
- `autostart_cyberdeck.sh` -> `/root/autostart_cyberdeck.sh`
- `config/sway/config` -> `/root/.config/sway/config`
- `config/wayvnc/config` -> `/root/.config/wayvnc/config`
- `systemd/cyberdeck.service` -> `/etc/systemd/system/cyberdeck.service`
- `systemd/wifi-ensure.service` -> `/etc/systemd/system/wifi-ensure.service`
- `systemd/wifi-ensure.timer` -> `/etc/systemd/system/wifi-ensure.timer`
- `systemd/calc-wifi-dhcp.service` -> `/etc/systemd/system/calc-wifi-dhcp.service`
- `cg-virtual-monitor/vnc-client` -> `/root/cg-virtual-monitor/vnc-client`

## Copy Files On The Pi

Run these commands from the cloned repo on the Raspberry Pi:

```bash
install -m 755 casio_ai.py /root/casio_ai.py
install -m 644 wifi_manager.py /root/wifi_manager.py
install -m 755 run_ai.sh /root/run_ai.sh
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
install -m 644 systemd/wifi-ensure.timer /etc/systemd/system/wifi-ensure.timer
install -m 644 systemd/calc-wifi-dhcp.service /etc/systemd/system/calc-wifi-dhcp.service
systemctl daemon-reload
systemctl enable cyberdeck.service
systemctl disable wifi-ensure.service || true
systemctl enable --now wifi-ensure.timer
```

## OpenAI API Key

The key is not in the source and never committed. `casio_ai.py` reads it from
`/root/.casio_ai.env`, which you must create by hand after a restore:

```bash
cat > /root/.casio_ai.env <<'EOF'
# API keys for casio_ai.py. Never commit this file.
OPENAI_API_KEY=your-openai-key-here
EOF
chmod 600 /root/.casio_ai.env
```

A real environment variable of the same name takes precedence over the file.
If a key is missing, the deck still starts and says so in the chat rather than
failing silently.

The only backend is `gpt-6.1-sol` at reasoning effort `high`, using the OpenAI
Responses API. Install the `openai` Python package; `google-genai` is no longer
needed. Existing `GEMINI_API_KEY` entries are ignored.

## Class Notes

The class-context feature reads markdown files from `/root/notes`, which is a
separate repo:

```bash
git clone https://github.com/evan-sie/casio-notes.git /root/notes
```

Without it the class selector has nothing to list.

Select a class at startup, then press Enter to load its notes into the GPT
conversation (or F2 to skip). Photos and text follow-ups continue that same
conversation. A program restart starts a fresh conversation, so load the notes
again. MECH4310 is currently installed; add `MECH3381.md` to this notes repo
when its course reference is ready to make it appear in the selector.

`previous_response_id` preserves conversation context, but earlier context can
still be billed as input or cached input on subsequent requests.

## Offline Verification

From the repo, with the runtime Python dependencies installed:

```bash
python3 -m unittest discover -s tests -v
```

The tests simulate OpenAI responses and block network connections. They do not
capture photos, load real course notes into a model, or spend API credits.

## Request Status and Stop

F3 stops an active request, retaining earlier conversation history. When idle,
F3 clears input. While stopping, the UI remains responsive but waits for the
old worker/connection to finish before accepting another request. Processing
already performed by the provider may still be billed.

Only OpenAI reasoning-summary events feed the thinking status. Answer events
feed the final answer regardless of whether that answer contains Markdown
headings. If the provider omits summaries, the status remains `GPT thinking`.

After a request, `/tmp/casio_ai_request_status.json` records event counts,
whether a summary arrived, and available reasoning-token usage. It is
overwritten per attempt and contains no prompts, images, keys, answers, or
summary text. This distinguishes missing provider summaries from display
problems using a normal request rather than paid diagnostic generations.

## DietPi Settings To Reapply

### Wi-Fi controls and recovery

The existing DietPi ifupdown configuration and
`/etc/wpa_supplicant/wpa_supplicant.conf` remain authoritative. This app never
reads passwords, changes that file, saves transient selections, or assigns a
static IP. Install `wpasupplicant`, `iproute2`, and `isc-dhcp-client` on restore.

Press F4 then F2 (SYM+F2) for live status/IP and saved networks. Up/Down selects,
Enter connects, F1 reconnects using the OS interface unit, F5 scans, and F2
returns immediately. Returning does not cancel a network operation or clear
chat history. This shortcut also works during the class-context confirmation.
On an offline startup, closing the normal help opens the network menu;
F2 continues offline. Neither the app nor its boot service waits for Wi-Fi.

The timer checks every 20 seconds. Healthy associations with an IPv4 address
and default route are left untouched. Failed connections get a 90-second grace
period, then bounded recovery with up to five-minute spacing. An in-progress
OS interface-start job is left alone. A shared lock prevents manual switching
and automatic repair from running together. Switching temporarily raises the
chosen profile's priority, leaves fallback profiles enabled, and obtains a new
DHCP lease for the new subnet. A separate service owns that DHCP client so
restarting the UI cannot kill it. Normal boot still uses the original DHCP
client started by ifupdown. No internet availability is inferred from Wi-Fi
association alone; API failures retain the existing retry/stop controls.

Network state transitions and relevant kernel/WPA/DHCP messages are retained
across reboot in `/var/lib/calc-network/events.log` (three files, 256 KiB each).
They contain network diagnostics, not API requests or Wi-Fi passwords.
`systemctl status wifi-ensure.timer` checks automatic recovery.
`run_ai.sh` restarts Python after an unexpected app crash while keeping the
terminal visible; it never resets the network. A crashed process loses its
in-memory chat, just as a manual program restart does.

For the saved `iPhone` hotspot, turn on Allow Others to Join and Maximize
Compatibility, and leave the Personal Hotspot screen open while connecting.
The saved SSID and password must match the phone. A new or renamed network
must first be provisioned through DietPi; the calculator menu reuses saved
profiles, including their existing enterprise certificate settings.

### Other settings

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
