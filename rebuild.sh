#!/bin/bash
# Set up a RackView display Pi: this code + a RackView backup (screens, settings, Pi setup).
#
# Usually started by the backup's restore.sh, which checks out the code version the backup recorded:
#   git clone <your backup repo> rackview-backup && bash rackview-backup/restore.sh
# Or by hand:  bash ~/rackview/rebuild.sh <unpacked backup folder>
#
# Start from Raspberry Pi OS 64-bit (Bookworm) WITH desktop, user/network set in Raspberry Pi Imager.
# Secrets are never in a backup: this asks for the Home Assistant token; re-enter the MQTT password in the
# builder (Settings), and add the Pi's NEW deploy key in GitHub if you use the git backup.
set -e
APP=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "${1:-.}" && pwd)
[ -f "$SRC/screens.json" ] || { echo "Usage: rebuild.sh <backup folder with screens.json, config.json, system/>"; exit 1; }
[ "$(id -u)" != 0 ] || { echo "Run this as the desktop user, not root (it uses sudo itself)."; exit 1; }
OLDHOME=$(python3 -c "import json;print(json.load(open('$SRC/system/manifest.json')).get('home') or '$HOME')" 2>/dev/null || echo "$HOME")

echo "== 1/7 packages"
sudo apt-get update -q
sudo apt-get install -y -q python3-pygame python3-flask python3-requests python3-paho-mqtt python3-pip python3-smbus \
    i2c-tools chrony wlr-randr git
[ -s "$SRC/system/pip-user.txt" ] && pip3 install --user --break-system-packages -q -r "$SRC/system/pip-user.txt"

echo "== 2/7 I2C (button board)"
sudo raspi-config nonint do_i2c 0

echo "== 3/7 timezone"
[ -s "$SRC/system/timezone" ] && sudo timedatectl set-timezone "$(cat "$SRC/system/timezone")"

echo "== 4/7 time sync (chrony to the GPS clock)"
if ls "$SRC"/system/chrony/*.sources >/dev/null 2>&1; then
    sudo mkdir -p /etc/chrony/sources.d
    sudo cp "$SRC"/system/chrony/*.sources /etc/chrony/sources.d/
    sudo systemctl disable --now systemd-timesyncd 2>/dev/null || true
    sudo systemctl enable chrony
    sudo systemctl restart chrony
fi

echo "== 5/7 RackView app and settings"
mkdir -p "$HOME/rackview" "$HOME/.config/rackview"
[ "$APP" = "$HOME/rackview" ] || cp -r "$APP"/. "$HOME/rackview/"
chmod +x "$HOME"/rackview/*.sh
python3 - "$SRC/config.json" "$HOME/.config/rackview/config.json" <<'PY'
import json, os, re, sys
src, dst = sys.argv[1:]
new = json.load(open(src))
try:
    cur = json.load(open(dst))
except (OSError, ValueError):
    cur = {}
new.update({k: v for k, v in cur.items() if re.search("pass|token|secret", k, re.I)})   # keep secrets already here
with open(os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    json.dump(new, f, indent=2)
PY
cp "$SRC/screens.json" "$HOME/.config/rackview/screens.json"
for f in notes.json known_devices.json; do [ -f "$SRC/$f" ] && cp "$SRC/$f" "$HOME/.config/rackview/$f"; done

echo "== 6/7 desktop (autostart RackView, no screensaver)"
for f in wayfire.ini kanshi/config; do
    if [ -f "$SRC/system/desktop/$f" ]; then
        mkdir -p "$(dirname "$HOME/.config/$f")"
        [ -f "$HOME/.config/$f" ] && cp "$HOME/.config/$f" "$HOME/.config/$f.before-rackview"
        sed "s#$OLDHOME#$HOME#g" "$SRC/system/desktop/$f" > "$HOME/.config/$f"
    fi
done
grep -q "rackview/run.sh" "$HOME/.config/wayfire.ini" 2>/dev/null || \
    printf '\n[autostart]\nrackview = %s/rackview/run.sh\n' "$HOME" >> "$HOME/.config/wayfire.ini"

echo "== 7/7 Home Assistant token"
if [ ! -s "$HOME/.config/rackview/ha_token" ]; then
    read -rsp "Paste a Home Assistant long-lived access token (HA profile → Security): " TOKEN; echo
    (umask 077; printf %s "$TOKEN" > "$HOME/.config/rackview/ha_token")
fi

echo
echo "Done. Reboot now (sudo reboot). After it starts:"
echo "  * open http://$(hostname -I | awk '{print $1}'):8088 and choose the builder password"
echo "  * Settings: re-enter the MQTT password if you publish to HA"
echo "  * Settings → Backup: the Pi made a NEW deploy key; add it in GitHub (and delete the old one)"
