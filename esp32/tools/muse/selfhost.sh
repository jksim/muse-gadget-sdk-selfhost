#!/usr/bin/env bash
# For developers: build, flash and watch the self-host gadget firmware from a
# checkout. Everyone else uses `musehost flash` on the Pi with a released build.
#
#   esp32/tools/muse/selfhost.sh build <board>      e.g. cores3 (see board.sh)
#   esp32/tools/muse/selfhost.sh flash <board> [port]
#   esp32/tools/muse/selfhost.sh log [port] [seconds] [--reset]   serial output, under logs/
#
# Nothing host-specific is built in: the host sends its CA and Noise key when
# the gadget is paired.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
fw=$(cd "$here/../.." && pwd)
root=$(cd "$fw/.." && pwd)
overlay=devices/sdkconfig.selfhost
default_port=/dev/ttyACM0

die() { echo "selfhost.sh: $*" >&2; exit 1; }

idf() {
    command -v idf.py >/dev/null 2>&1 && return
    local export_sh="${IDF_EXPORT:-$HOME/esp/esp-idf-v6.0.1/export.sh}"
    [ -f "$export_sh" ] || die "ESP-IDF v6.0.1 not found; set IDF_EXPORT"
    # shellcheck disable=SC1090
    . "$export_sh" >/dev/null 2>&1
}

cmd=${1:-}; shift || true
case $cmd in
    build|flash)
        board=${1:?board, e.g. cores3}
        idf
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
