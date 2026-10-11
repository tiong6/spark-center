#!/bin/sh
# Run as root after reviewing docs/STABILITY.md. Uses the distribution package.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
[ "$(id -u)" = 0 ] || { echo 'Run with sudo in your terminal; never share your password.'; exit 1; }
[ -f "$ROOT/data/memory-policy.json" ] || {
    echo "Calibrate first / 請先校準：python3 '$ROOT/tools/calibrate_memory.py'" >&2
    exit 1
}
LIMITS=$(python3 - "$ROOT/data/memory-policy.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1])); floor=p['min_available_bytes']; total=p['mem_total_bytes']
assert isinstance(floor,int) and 0 < floor < total//3
actual=int(next(l.split()[1] for l in open('/proc/meminfo') if l.startswith('MemTotal:')))*1024
assert total==actual, 'Memory capacity changed; recalibrate first'
# Admission refuses first; emergency TERM/KILL occur at half/quarter that reserve.
print(str(floor//2048)+','+str(floor//4096))
PY
)
if systemctl is-active --quiet systemd-oomd; then
    echo 'systemd-oomd already active: stop and review ownership before installing another OOM daemon.'
    exit 1
fi
apt-get -o Dpkg::Options::=--force-confold install -y earlyoom
if [ -f /etc/default/earlyoom ]; then
    cp -p /etc/default/earlyoom "/etc/default/earlyoom.before-spark-$(date +%s)"
fi
# No spaces/quotes inside regex: systemd expands EARLYOOM_ARGS directly.
printf 'EARLYOOM_ARGS="-M %s -s 100,100 -r 60 --prefer ^(strata.*|ollama|llama.*)$ --avoid ^(gnome-shell|Xorg|Xwayland|spark-center|spark-guard)$"\n' "$LIMITS" > /etc/default/earlyoom
systemctl enable earlyoom.service
systemctl restart earlyoom.service
systemctl --no-pager --full status earlyoom.service
