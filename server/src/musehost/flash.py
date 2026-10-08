"""Put the self-host firmware on a gadget plugged into this machine's USB.

The firmware is built once per board by the SDK fork's release workflow and
published as `selfhost-v<version>` GitHub releases. Each zip holds the images a
build flashes plus manifest.json (board, chip, version, flash settings, and per
image its offset, size and SHA-256).

Every image is written at its own offset: never one merged image, because the
settings partition (NVS, holding Wi-Fi and the pairing) sits between the
partition table and otadata and a merged image would wipe it. `erase_settings`
erases exactly that partition, read from the firmware's own partition table.
The factory data partitions (prod_data, prod_bak) are never written or erased.
Nothing here runs in the service; only the `musehost flash` command uses it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

TAG_PREFIX = "selfhost-v"
ESPRESSIF_USB_VID = 0x303A
BAUD = "460800"
PROTECTED = ("prod_data", "prod_bak")
API = "https://api.github.com/repos/{repo}/releases?per_page=100"
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?")


class FlashError(Exception):
    """Anything that stops a flash; the message is for the person running it."""


@dataclass(frozen=True)
class Image:
    offset: int
    name: str
    data: bytes


@dataclass(frozen=True)
class Partition:
    label: str
    type: int
    subtype: int
    offset: int
    size: int


@dataclass(frozen=True)
class Firmware:
    board: str
    chip: str
    version: str
    flash: dict
    images: list[Image]
    partitions: list[Partition]
    nvs: tuple[int, int]


@dataclass(frozen=True)
class Asset:
    version: str
    name: str
    url: str


# -- the firmware zip ------------------------------------------------------------------


def parse_partition_table(data: bytes) -> list[Partition]:
    parts = []
    for at in range(0, len(data) - 31, 32):
        entry = data[at : at + 32]
        if entry[:2] == b"\xaa\x50":
            ptype, subtype, offset, size = struct.unpack_from("<BBLL", entry, 2)
            label = entry[12:28].split(b"\0", 1)[0].decode("ascii", "replace")
            parts.append(Partition(label, ptype, subtype, offset, size))
        elif entry[:2] in (b"\xeb\xeb", b"\xff\xff"):  # MD5 entry, or the end
            break
        else:
            raise FlashError("the firmware's partition table is unreadable")
    return parts


def _overlaps(a_start: int, a_size: int, b_start: int, b_size: int) -> bool:
    return a_start < b_start + b_size and b_start < a_start + a_size


def load_firmware(path: Path, *, board: str) -> Firmware:
    """Read and check a release zip; raises FlashError before anything is written."""
    try:
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read("manifest.json"))
            entries = {name: z.read(name) for name in z.namelist()}
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise FlashError(f"{path} is not a firmware zip ({exc})") from None
    if manifest.get("format") != 1:
        raise FlashError(f"{path}: unknown manifest format {manifest.get('format')!r}")
    if manifest.get("board") != board:
        raise FlashError(f"that firmware is for {manifest.get('board')!r}, not {board!r}")

    images = []
    roles = {}
    for item in manifest.get("files", []):
        name = str(item.get("name", ""))
        if not SAFE_NAME.fullmatch(name) or ".." in name or name not in entries:
            raise FlashError(f"{path}: bad or missing image {name!r}")
        data = entries[name]
        if len(data) != item.get("size") or hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise FlashError(f"{name}: checksum doesn't match the manifest; not flashing it")
        images.append(Image(int(str(item["offset"]), 16), name, data))
        roles[item.get("role")] = data
    if not images:
        raise FlashError(f"{path}: the manifest lists no images")
    images.sort(key=lambda i: i.offset)
    for a, b in zip(images, images[1:], strict=False):
        if a.offset + len(a.data) > b.offset:
            raise FlashError(f"{a.name} and {b.name} overlap")

    if "partition-table" not in roles:
        raise FlashError(f"{path}: no partition table")
    partitions = parse_partition_table(roles["partition-table"])
    nvs = next((p for p in partitions if p.label == "nvs" and p.type == 1 and p.subtype == 2), None)
    if nvs is None:
        raise FlashError("the firmware's partition table has no nvs (settings) partition")
    protected = [p for p in partitions if p.label in PROTECTED]
    for p in protected:
        if _overlaps(nvs.offset, nvs.size, p.offset, p.size):
            raise FlashError(f"the nvs partition overlaps {p.label}; refusing to flash")
    for image in images:
        for p in protected:
            if _overlaps(image.offset, len(image.data), p.offset, p.size):
                raise FlashError(f"{image.name} would overwrite {p.label}; refusing to flash")
        if _overlaps(image.offset, len(image.data), nvs.offset, nvs.size):
            raise FlashError(f"{image.name} would overwrite the settings (nvs); refusing")

    flash = manifest.get("flash", {})
    if not all(flash.get(k) for k in ("mode", "freq", "size")) or not manifest.get("chip"):
        raise FlashError(f"{path}: the manifest lacks the chip or flash settings")
    return Firmware(
        board=board,
        chip=manifest["chip"],
        version=str(manifest.get("version", "?")),
        flash=flash,
        images=images,
        partitions=partitions,
        nvs=(nvs.offset, nvs.size),
    )


# -- releases ----------------------------------------------------------------------------


def _version_key(version: str) -> tuple:
    m = VERSION.fullmatch(version)
    if not m:
        return (-1,)
    major, minor, patch, pre = m.groups()
    # A release sorts after its own pre-releases.
    return (int(major), int(minor), int(patch), pre is None, pre or "")


def choose_release(api: Iterable[dict], *, board: str, version: str | None) -> Asset:
    """The newest `selfhost-v*` release with firmware for `board`, or `version`."""
    wanted = version.removeprefix(TAG_PREFIX).removeprefix("v") if version else None
    found = []
    for release in api:
        tag = str(release.get("tag_name", ""))
        if release.get("draft") or not tag.startswith(TAG_PREFIX):
            continue
        ver = tag.removeprefix(TAG_PREFIX)
        if VERSION.fullmatch(ver) is None or (wanted and ver != wanted):
            continue
        name = f"muse-gadget-selfhost-{board}-{ver}.zip"
        for asset in release.get("assets", []):
            if asset.get("name") == name:
                found.append(Asset(ver, name, asset["browser_download_url"]))
    if not found:
        which = f"{wanted} " if wanted else ""
        raise FlashError(f"no self-host firmware {which}for {board} in the {TAG_PREFIX}* releases")
    return max(found, key=lambda a: _version_key(a.version))


def http_get(url: str) -> bytes:
    if not url.startswith("https://"):
        raise FlashError(f"refusing to download from {url!r}: not https")
    request = urllib.request.Request(  # noqa: S310 (https only, checked above)
        url, headers={"User-Agent": "musehost", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        return response.read()


def fetch_release(
    repo: str,
    *,
    board: str,
    version: str | None,
    cache: Path,
    fetch: Callable[[str], bytes],
) -> Path:
    """Download the chosen release zip into `cache` once; returns its path."""
    api = json.loads(fetch(API.format(repo=repo)))
    asset = choose_release(api, board=board, version=version)
    target = cache / asset.name
    if target.exists():
        return target
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = fetch(asset.url)
    with tempfile.NamedTemporaryFile(dir=cache, suffix=".part", delete=False) as tmp:
        tmp.write(data)
    Path(tmp.name).replace(target)
    return target


# -- the gadget --------------------------------------------------------------------------


def serial_ports() -> list:
    from serial.tools import list_ports

    return list(list_ports.comports())


def find_port(port: str | None, *, list_ports: Callable[[], list]) -> str:
    if port:
        return port
    gadgets = [p.device for p in list_ports() if p.vid == ESPRESSIF_USB_VID]
    if not gadgets:
        raise FlashError("no gadget found on USB; plug it in (a data cable) or pass --port")
    if len(gadgets) > 1:
        raise FlashError(f"several gadgets on USB; choose one with --port: {', '.join(gadgets)}")
    return gadgets[0]


def _esptool_command(args: list[str]) -> list[str]:
    # Our own interpreter running esptool; the arguments are checked hex offsets,
    # temp file paths, and the port and settings from a verified manifest.
    return [sys.executable, "-m", "esptool", *args]


def run_esptool(args: list[str]) -> int:
    """Runs esptool with its progress on this terminal; returns its exit status."""
    cmd = _esptool_command(args)
    # esptool reads esptool.cfg from its working directory: run it in an empty
    # one, not wherever musehost was started (which may not even be readable).
    with tempfile.TemporaryDirectory() as cwd:
        return subprocess.run(cmd, check=False, cwd=cwd).returncode  # noqa: S603


PROGRESS_EVERY_S = 2.0


def run_esptool_lines(args: list[str], on_line: Callable[[str], None], holder=None) -> int:
    """Runs esptool through a pipe, giving ``on_line`` each line it prints.

    Image paths (the temp files) are shown by name only, and progress lines
    ("... 42 %") at most every couple of seconds. ``holder``, a list, gets the
    process, so a stopped job can kill it.
    """
    names = {a: Path(a).name for a in args if os.sep in a and Path(a).is_file()}
    last_progress = 0.0

    def emit(raw: bytes) -> None:
        nonlocal last_progress
        line = raw.decode(errors="replace").strip()
        if not line:
            return
        for path, name in names.items():
            line = line.replace(path, name)
        if "%" in line and "100" not in line:
            now = time.monotonic()
            if now - last_progress < PROGRESS_EVERY_S:
                return
            last_progress = now
        on_line(line)

    with tempfile.TemporaryDirectory() as cwd:  # not the caller's esptool.cfg; see run_esptool
        proc = subprocess.Popen(  # noqa: S603 - see _esptool_command
            _esptool_command(args),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if holder is not None:
            holder.append(proc)
        pending = b""
        for chunk in iter(lambda: proc.stdout.read1(512), b""):
            *lines, pending = re.split(rb"[\r\n]", pending + chunk)
            for raw in lines:
                emit(raw)
        emit(pending)
        return proc.wait()


def prepare(
    config,
    state: Path,
    *,
    board: str,
    version: str | None = None,
    file: str | None = None,
    port: str | None = None,
    progress: Callable[[str], None] = print,
) -> tuple[Firmware, str]:
    """The checked firmware (a release, or ``file``) and the gadget's port.

    Shared by ``musehost flash`` and the dashboard's flash job; raises
    FlashError, or OSError for network trouble or an unreadable file.
    """
    if file:
        path = Path(file)
    else:
        progress(f"Looking for {board} firmware in {config.firmware_repo} releases...")
        path = fetch_release(
            config.firmware_repo,
            board=board,
            version=version,
            cache=state / "firmware",
            fetch=http_get,
        )
    fw = load_firmware(path, board=board)
    return fw, find_port(port, list_ports=serial_ports)


def write(
    fw: Firmware,
    port: str,
    *,
    erase_settings: bool,
    esptool: Callable[[list[str]], int] | None = None,
) -> None:
    esptool = esptool or run_esptool
    base = ["--chip", fw.chip, "--port", port, "--baud", BAUD, "--before", "default-reset"]
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for image in fw.images:
            path = Path(tmp) / image.name
            path.write_bytes(image.data)
            files += [hex(image.offset), str(path)]
        if erase_settings:
            offset, size = fw.nvs
            if esptool([*base, "--after", "no-reset", "erase-region", hex(offset), hex(size)]):
                raise FlashError("erasing the settings failed; see esptool's message above")
        flash = fw.flash
        args = [
            *base,
            "--after",
            "hard-reset",
            "write-flash",
            "--flash-mode",
            flash["mode"],
            "--flash-freq",
            flash["freq"],
            "--flash-size",
            flash["size"],
            *files,
        ]
        if esptool(args):
            raise FlashError("writing the firmware failed; see esptool's message above")
