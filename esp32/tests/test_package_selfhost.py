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
"""tools/muse/package_selfhost.py: a self-host build becomes a release zip."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "muse" / "package_selfhost.py"

FILES = {
    "0x0": "bootloader/bootloader.bin",
    "0x10000": "partition_table/partition-table.bin",
    "0x1d000": "ota_data_initial.bin",
    "0x20000": "muse-gadget.bin",
}


def fake_build(
    root: Path, chip: str = "esp32s3", encrypted: str = "false", app_version: str = "0.1.0"
) -> Path:
    build = root / "build-muse-m5stack-cores3-selfhost"
    for offset, name in FILES.items():
        path = build / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"image at {offset}".encode() * 100)
    roles = {"0x0": "bootloader", "0x10000": "partition-table", "0x1d000": "otadata", "0x20000": "app"}
    flasher = {
        "write_flash_args": ["--flash-mode", "dio", "--flash-size", "keep", "--flash-freq", "80m"],
        "flash_settings": {"flash_mode": "dio", "flash_size": "keep", "flash_freq": "80m"},
        "flash_files": FILES,
        "extra_esptool_args": {"after": "hard-reset", "before": "default-reset", "stub": True, "chip": chip},
    }
    for offset, role in roles.items():
        flasher[role] = {"offset": offset, "file": FILES[offset], "encrypted": encrypted}
    (build / "flasher_args.json").write_text(json.dumps(flasher))
    (build / "project_description.json").write_text(json.dumps({"project_version": app_version}))
    return build


def package(build: Path, out: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(build), *args, str(out)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class PackageSelfhostTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_zip_holds_each_image_and_a_manifest_that_matches_them(self) -> None:
        build = fake_build(self.tmp)
        out = self.tmp / "dist"
        proc = package(build, out, "cores3", "0.1.0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        zip_path = out / "muse-gadget-selfhost-cores3-0.1.0.zip"
        self.assertEqual(proc.stdout.strip(), str(zip_path))
        with zipfile.ZipFile(zip_path) as z:
            manifest = json.loads(z.read("manifest.json"))
            self.assertEqual(
                {k: manifest[k] for k in ("format", "board", "chip", "version")},
                {"format": 1, "board": "cores3", "chip": "esp32s3", "version": "0.1.0"},
            )
            self.assertEqual(manifest["flash"], {"mode": "dio", "freq": "80m", "size": "keep"})
            self.assertEqual(
                [(f["offset"], f["name"]) for f in manifest["files"]],
                [("0x0", "bootloader.bin"), ("0x10000", "partition-table.bin"),
                 ("0x1d000", "ota_data_initial.bin"), ("0x20000", "muse-gadget.bin")],
            )
            for f in manifest["files"]:
                data = z.read(f["name"])
                self.assertEqual(f["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(f["size"], len(data))
                self.assertEqual(data, (build / FILES[f["offset"]]).read_bytes())
            self.assertEqual(sorted(z.namelist()), sorted(["manifest.json", *(f["name"] for f in manifest["files"])]))

    def test_refuses_a_missing_image(self) -> None:
        build = fake_build(self.tmp)
        (build / "muse-gadget.bin").unlink()
        proc = package(build, self.tmp / "dist", "cores3", "0.1.0")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("muse-gadget.bin", proc.stderr)
        self.assertFalse((self.tmp / "dist").exists() and any((self.tmp / "dist").iterdir()))

    def test_refuses_encrypted_images(self) -> None:
        build = fake_build(self.tmp, encrypted="true")
        proc = package(build, self.tmp / "dist", "cores3", "0.1.0")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("encrypted", proc.stderr)

    def test_refuses_a_version_that_is_not_semver(self) -> None:
        build = fake_build(self.tmp)
        for version in ("v0.1.0", "0.1", "latest", "0.1.0/../x"):
            with self.subTest(version=version):
                proc = package(build, self.tmp / "dist", "cores3", version)
                self.assertNotEqual(proc.returncode, 0)

    def test_refuses_a_build_whose_app_reports_another_version(self) -> None:
        # The app's own version (from version.txt) is what the gadget reports to the
        # host; a release labelled 0.1.0 must not carry upstream's 999.0.0.
        build = fake_build(self.tmp, app_version="999.0.0")
        proc = package(build, self.tmp / "dist", "cores3", "0.1.0")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("999.0.0", proc.stderr)
        (build / "project_description.json").unlink()
        self.assertNotEqual(package(build, self.tmp / "dist", "cores3", "0.1.0").returncode, 0)

    def test_refuses_a_board_name_that_is_not_simple(self) -> None:
        proc = package(fake_build(self.tmp), self.tmp / "dist", "../cores3", "0.1.0")
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
