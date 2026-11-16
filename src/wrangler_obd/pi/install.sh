#!/bin/bash
# Sets up the Wrangler in-Jeep logger on a Raspberry Pi running Raspberry Pi OS.
#
# The PC app runs this for you (In-Jeep logger tab, "Install on Pi"): it copies
# the files to /tmp/wrangler-install first, then runs this as root. Running it
# again updates the logger and keeps the drives already recorded.
#
#   sudo bash /tmp/wrangler-install/pi/install.sh --power lifepo4wered|none
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
POWER="none"
while [ $# -gt 0 ]; do
  case "$1" in
    --power) POWER="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done

step() { echo; echo "== $*"; }

if [ "$(id -u)" -ne 0 ]; then echo "Run this with sudo."; exit 1; fi

step "Installing what the logger needs (Python serial support)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-serial

if [ "$POWER" = "lifepo4wered" ]; then
  step "Installing the LiFePO4wered/Pi+ power board software"
  apt-get install -y -q build-essential git libsystemd-dev
  if [ -d /opt/LiFePO4wered-Pi/.git ]; then
    git -C /opt/LiFePO4wered-Pi pull -q --ff-only || true
  else
    git clone -q https://github.com/xorbit/LiFePO4wered-Pi.git /opt/LiFePO4wered-Pi
  fi
  make -C /opt/LiFePO4wered-Pi all
  make -C /opt/LiFePO4wered-Pi user-install
fi

step "Copying the logger"
systemctl stop wrangler-logger 2>/dev/null || true
rm -rf /opt/wrangler/src/wrangler_obd
mkdir -p /opt/wrangler/src /var/lib/wrangler
cp -r "$SRC/wrangler_obd" /opt/wrangler/src/
install -m 600 "$SRC/wrangler-logger.json" /etc/wrangler-logger.json
install -m 644 "$SRC/pi/wrangler-logger.service" /etc/systemd/system/wrangler-logger.service
python3 -c "import sys; sys.path.insert(0, '/opt/wrangler/src'); import wrangler_obd.carlogger"

step "Protecting the SD card"
# The system journal lives in memory, not on the card.
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/wrangler.conf <<CONF
[Journal]
Storage=volatile
RuntimeMaxUse=20M
CONF
# No swap file on the card (memory-based swap, if any, is fine).
if systemctl list-unit-files dphys-swapfile.service >/dev/null 2>&1; then
  systemctl disable --now dphys-swapfile.service 2>/dev/null || true
  dphys-swapfile swapoff 2>/dev/null || true
  dphys-swapfile uninstall 2>/dev/null || true
fi
# No background package downloads and upgrades writing to the card while driving.
for t in apt-daily.timer apt-daily-upgrade.timer man-db.timer; do
  systemctl disable --now "$t" 2>/dev/null || true
done
# Don't record every file read on the card.
if ! awk '$2 == "/" { print $4 }' /etc/fstab | grep -q noatime; then
  sed -i -E 's|^(\S+\s+/\s+\S+\s+)(\S+)|\1\2,noatime|' /etc/fstab
fi
# Repair the file system automatically at start-up if it ever needs it.
for f in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
  if [ -f "$f" ]; then
    grep -q "fsck.repair=yes" "$f" || sed -i '1 s/$/ fsck.repair=yes/' "$f"
    break
  fi
done

step "Starting the logger at every boot"
systemctl daemon-reload
systemctl enable wrangler-logger.service

step "All set. Restarting the Pi in a few seconds."
systemd-run --on-active=5s /bin/systemctl reboot >/dev/null
