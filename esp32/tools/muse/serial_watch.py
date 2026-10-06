"""Log a gadget's USB serial output, surviving resets and re-enumeration.

    python esp32/tools/muse/serial_watch.py PORT SECONDS OUT

Each (re)open and loss of the port is marked with a wall-clock line, so
reboots show up in the log instead of silently ending it.
"""

import sys
import time

import serial

port, seconds, out = sys.argv[1], float(sys.argv[2]), sys.argv[3]
end = time.monotonic() + seconds
with open(out, "ab") as f:

    def mark(event: str) -> None:
        f.write(f"\n=== {time.strftime('%H:%M:%S')} {event} ===\n".encode())
        f.flush()

    while time.monotonic() < end:
        try:
            s = serial.Serial(port, 115200, timeout=0.2)
        except (OSError, serial.SerialException):
            time.sleep(0.2)
            continue
        mark("port opened")
        try:
            while time.monotonic() < end:
                data = s.read(4096)
                if data:
                    f.write(data)
                    f.flush()
        except (OSError, serial.SerialException):
            mark("port lost")
        finally:
            s.close()
