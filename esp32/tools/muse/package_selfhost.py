#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Package a self-host firmware build as a release zip.

    package_selfhost.py BUILD_DIR BOARD VERSION OUT_DIR

BUILD_DIR is a `MUSE_EXTRA_DEFAULTS=devices/sdkconfig.selfhost
tools/muse/board.sh build BOARD` folder. The zip, named
muse-gadget-selfhost-BOARD-VERSION.zip, holds each image the build would flash
plus manifest.json: board, chip, version, flash settings, and per image its
offset, size and SHA-256. `musehost flash` writes the images at their own
offsets, never as one merged image, so the pairing in NVS (which sits between
the partition table and otadata) survives an update.

Prints the zip's path.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

ROLES = ("bootloader", "partition-table", "otadata", "app")


def fail(message: str) -> None:
    print(f"package_selfhost.py: {message}", file=sys.stderr)
    sys.exit(1)


def main(argv: list[str]) -> None:
    if len(argv) != 4:
        fail("usage: package_selfhost.py BUILD_DIR BOARD VERSION OUT_DIR")
    build, board, version, out = Path(argv[0]), argv[1], argv[2], Path(argv[3])
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", board):
        fail(f"board must be a simple name like cores3, not {board!r}")
    if not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", version):
        fail(f"version must look like 1.2.3, not {version!r}")
    # The gadget reports the app's own version (version.txt) to the host; it
    # must be the release's, not upstream's 999.0.0 placeholder.
    try:
        app_version = json.loads((build / "project_description.json").read_text())[
            "project_version"
        ]
    except (OSError, ValueError, KeyError) as exc:
        fail(f"no project_version in {build}/project_description.json: {exc}")
    if app_version != version:
        fail(f"the app was built as version {app_version}, not {version}; set esp32/version.txt")
    try:
        flasher = json.loads((build / "flasher_args.json").read_text())
    except (OSError, ValueError) as exc:
        fail(f"no usable flasher_args.json in {build}: {exc}")

    files = []
    for role in ROLES:
        entry = flasher.get(role)
        if not entry:
            fail(f"flasher_args.json has no {role}")
        if str(entry.get("encrypted", "false")).lower() != "false":
            fail(f"{role} is encrypted; release builds must not be")
        path = build / entry["file"]
        if not path.is_file():
            fail(f"missing {path}")
        data = path.read_bytes()
        files.append(
            {
                "role": role,
                "name": path.name,
                "offset": entry["offset"],
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "_path": path,
            }
        )
    if len({f["name"] for f in files}) != len(files):
        fail("two images share a file name")

    settings = flasher.get("flash_settings", {})
    manifest = {
        "format": 1,
        "board": board,
        "chip": flasher.get("extra_esptool_args", {}).get("chip", ""),
        "version": version,
        "flash": {
            "mode": settings.get("flash_mode", ""),
            "freq": settings.get("flash_freq", ""),
            "size": settings.get("flash_size", ""),
        },
        "files": [{k: v for k, v in f.items() if not k.startswith("_")} for f in files],
    }
    if not manifest["chip"] or not all(manifest["flash"].values()):
        fail("flasher_args.json lacks the chip or flash settings")

    out.mkdir(parents=True, exist_ok=True)
    target = out / f"muse-gadget-selfhost-{board}-{version}.zip"
    # Write beside the target, then rename: never leave a half-written release.
    with tempfile.NamedTemporaryFile(dir=out, suffix=".tmp", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
            for f in files:
                z.write(f["_path"], f["name"])
        tmp_path.replace(target)
    finally:
        tmp_path.unlink(missing_ok=True)
    print(target)


if __name__ == "__main__":
    main(sys.argv[1:])
