#!/usr/bin/env bash
# Build, flash and watch Muse gadget firmware that talks to our own host.
#
#   build.sh fetch-ca [user@host]           copy the host's CA into the firmware tree
#   build.sh build <board>                  e.g. cores3 (see tools/muse/board.sh)
#   build.sh flash <board> [port]
#   build.sh log [port] [seconds] [--reset] save serial output under logs/
#
# The CA is compiled into the firmware's certificate bundle, so a new host CA
# (musehost init --force) means fetch-ca, build and flash again.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
fw="$root/muse-gadget-sdk-selfhost/esp32"
ca="$fw/selfhost/ca.pem"
overlay=devices/sdkconfig.selfhost
default_host=${MUSEHOST_PI:-muse-host.local}
default_port=/dev/ttyACM0

die() { echo "build.sh: $*" >&2; exit 1; }

idf() {
    command -v idf.py >/dev/null 2>&1 && return
    local export_sh="${IDF_EXPORT:-$HOME/esp/esp-idf-v6.0.1/export.sh}"
    [ -f "$export_sh" ] || die "ESP-IDF v6.0.1 not found; set IDF_EXPORT"
    # shellcheck disable=SC1090
    . "$export_sh" >/dev/null 2>&1
}

fingerprint() { openssl x509 -noout -fingerprint -sha256 -in "$1" | cut -d= -f2; }

need_ca() {
    [ -s "$ca" ] || die "no $ca; run: build.sh fetch-ca"
    openssl x509 -noout -in "$ca" 2>/dev/null || die "$ca is not a certificate"
}

cmd=${1:-}; shift || true
case $cmd in
    fetch-ca)
        mkdir -p "$(dirname "$ca")"
        # install.sh keeps a world-readable copy of the (public) CA certificate.
        ssh "${1:-$default_host}" "cat /opt/musehost/ca.pem" > "$ca.tmp"
        openssl x509 -noout -in "$ca.tmp" 2>/dev/null || { rm -f "$ca.tmp"; die "fetched file is not a certificate"; }
        mv "$ca.tmp" "$ca"
        echo "CA SHA-256: $(fingerprint "$ca")"
        ;;
    build|flash)
        board=${1:?board, e.g. cores3}
        need_ca
        idf
        echo "Firmware will trust CA SHA-256: $(fingerprint "$ca")"
        if [ "$cmd" = build ]; then
            MUSE_EXTRA_DEFAULTS=$overlay "$fw/tools/muse/board.sh" build "$board"
        else
            MUSE_EXTRA_DEFAULTS=$overlay "$fw/tools/muse/board.sh" flash "$board" "${2:-$default_port}"
        fi
        ;;
    log)
        port=${1:-$default_port}; seconds=${2:-30}; reset=${3:-}
        idf
        mkdir -p "$root/logs"
        out="$root/logs/serial-$(date +%Y%m%d-%H%M%S).log"
        python - "$port" "$seconds" "$out" "$reset" <<'PY'
import sys, time
import serial

port, seconds, out, reset = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
s = serial.Serial()
s.port, s.baudrate, s.timeout = port, 115200, 0.2
s.dtr = s.rts = False  # opening must not reset the chip unless asked
s.open()
if reset == "--reset":
    s.rts = True; time.sleep(0.1); s.rts = False
end = time.monotonic() + seconds
with open(out, "wb") as f:
    while time.monotonic() < end:
        f.write(s.read(4096))
s.close()
print(out)
PY
        ;;
    *)
        sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
        exit 2
        ;;
esac
