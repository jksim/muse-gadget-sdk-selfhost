"""musehost flash: choose, verify and write a self-host firmware release.

No device or network: the GitHub API, the serial ports and esptool are fakes.
The zips are real, with a real ESP-IDF partition table inside.
"""

import hashlib
import io
import json
import struct
import zipfile
from types import SimpleNamespace

import pytest

from musehost import flash

NVS = (0x11000, 0xC000)


def partition_table(extra=()) -> bytes:
    """An ESP-IDF partition table like the CoreS3's (partitions_muse.csv)."""
    rows = [
        ("nvs", 1, 0x02, *NVS),
        ("otadata", 1, 0x00, 0x1D000, 0x2000),
        ("phy_init", 1, 0x01, 0x1F000, 0x1000),
        ("ota_0", 0, 0x10, 0x20000, 0x400000),
        ("ota_1", 0, 0x11, 0x420000, 0x400000),
        ("prod_data", 1, 0x40, 0x820000, 0x1000),
        ("prod_bak", 1, 0x41, 0x821000, 0x1000),
        *extra,
    ]
    out = b""
    for label, ptype, subtype, offset, size in rows:
        out += struct.pack(
            "<2sBBLL16sL",
            b"\xaa\x50",
            ptype,
            subtype,
            offset,
            size,
            label.encode().ljust(16, b"\0"),
            0,
        )
    out += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(out, usedforsecurity=False).digest()
    return out + b"\xff" * (0xC00 - len(out))


def make_zip(
    path,
    *,
    board="cores3",
    version="0.2.0",
    chip="esp32s3",
    tamper=None,
    offsets=None,
    table=None,
    names=None,
):
    images = {
        "bootloader.bin": (0x0, b"boot" * 64),
        "partition-table.bin": (0x10000, table or partition_table()),
        "ota_data_initial.bin": (0x1D000, b"\xff" * 0x2000),
        "muse-gadget.bin": (0x20000, b"app!" * 4096),
    }
    if offsets:
        for name, offset in offsets.items():
            images[name] = (offset, images[name][1])
    roles = dict(zip(images, ("bootloader", "partition-table", "otadata", "app"), strict=True))
    manifest = {
        "format": 1,
        "board": board,
        "chip": chip,
        "version": version,
        "flash": {"mode": "dio", "freq": "80m", "size": "keep"},
        "files": [
            {
                "role": roles[name],
                "name": (names or {}).get(name, name),
                "offset": hex(offset),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, (offset, data) in images.items()
        ],
    }
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for name, (_, data) in images.items():
            z.writestr((names or {}).get(name, name), (b"X" + data[1:]) if name == tamper else data)
    return path


# -- loading and checking a firmware zip --------------------------------------------------


def test_a_good_zip_loads_with_its_images_and_nvs_region(tmp_path):
    fw = flash.load_firmware(make_zip(tmp_path / "fw.zip"), board="cores3")
    assert (fw.board, fw.chip, fw.version) == ("cores3", "esp32s3", "0.2.0")
    assert [(i.offset, i.name) for i in fw.images] == [
        (0x0, "bootloader.bin"),
        (0x10000, "partition-table.bin"),
        (0x1D000, "ota_data_initial.bin"),
        (0x20000, "muse-gadget.bin"),
    ]
    assert fw.nvs == NVS


def test_a_tampered_image_is_refused(tmp_path):
    with pytest.raises(flash.FlashError, match="muse-gadget.bin.*checksum"):
        flash.load_firmware(make_zip(tmp_path / "fw.zip", tamper="muse-gadget.bin"), board="cores3")


def test_firmware_for_another_board_is_refused(tmp_path):
    with pytest.raises(flash.FlashError, match="sticks3.*cores3"):
        flash.load_firmware(make_zip(tmp_path / "fw.zip", board="sticks3"), board="cores3")


def test_an_image_over_the_factory_data_is_refused(tmp_path):
    zip_path = make_zip(tmp_path / "fw.zip", offsets={"muse-gadget.bin": 0x820000})
    with pytest.raises(flash.FlashError, match="prod_data"):
        flash.load_firmware(zip_path, board="cores3")


def test_an_image_over_the_settings_is_refused(tmp_path):
    zip_path = make_zip(tmp_path / "fw.zip", offsets={"ota_data_initial.bin": 0x12000})
    with pytest.raises(flash.FlashError, match="nvs"):
        flash.load_firmware(zip_path, board="cores3")


def test_a_zip_with_unsafe_names_is_refused(tmp_path):
    zip_path = make_zip(tmp_path / "fw.zip", names={"muse-gadget.bin": "../muse-gadget.bin"})
    with pytest.raises(flash.FlashError):
        flash.load_firmware(zip_path, board="cores3")


def test_not_a_zip_is_refused(tmp_path):
    (tmp_path / "fw.zip").write_text("hello")
    with pytest.raises(flash.FlashError, match="not a firmware zip"):
        flash.load_firmware(tmp_path / "fw.zip", board="cores3")


# -- choosing a release ------------------------------------------------------------------


def releases(*tags, drafts=(), boards=("cores3",)):
    return [
        {
            "tag_name": tag,
            "draft": tag in drafts,
            "assets": [
                {
                    "name": f"muse-gadget-selfhost-{b}-{tag.removeprefix('selfhost-v')}.zip",
                    "browser_download_url": f"https://example/{tag}/{b}.zip",
                }
                for b in boards
            ],
        }
        for tag in tags
    ]


def test_the_newest_selfhost_release_is_chosen():
    api = releases(
        "selfhost-v0.2.0",
        "selfhost-v0.10.0",
        "selfhost-v0.9.1",
        "v99.0.0",
        "selfhost-v1.0.0",
        drafts=("selfhost-v1.0.0",),
    )
    asset = flash.choose_release(api, board="cores3", version=None)
    assert (asset.version, asset.name) == ("0.10.0", "muse-gadget-selfhost-cores3-0.10.0.zip")


def test_a_named_version_is_chosen():
    asset = flash.choose_release(
        releases("selfhost-v0.2.0", "selfhost-v0.3.0"), board="cores3", version="0.2.0"
    )
    assert asset.version == "0.2.0"
    assert (
        flash.choose_release(
            releases("selfhost-v0.2.0"), board="cores3", version="selfhost-v0.2.0"
        ).version
        == "0.2.0"
    )


def test_no_matching_release_explains_itself():
    with pytest.raises(flash.FlashError, match="no self-host firmware"):
        flash.choose_release(releases("v1.0.0"), board="cores3", version=None)
    with pytest.raises(flash.FlashError, match="cores3"):
        flash.choose_release(
            releases("selfhost-v0.2.0", boards=("sticks3",)), board="cores3", version=None
        )


def test_a_release_is_downloaded_once_and_cached(tmp_path):
    zip_bytes = make_zip(tmp_path / "src.zip").read_bytes()
    calls = []

    def fetch(url):
        calls.append(url)
        if url.startswith("https://api.github.com/"):
            return json.dumps(releases("selfhost-v0.2.0")).encode()
        return zip_bytes

    cache = tmp_path / "firmware"
    first = flash.fetch_release("me/fw", board="cores3", version=None, cache=cache, fetch=fetch)
    second = flash.fetch_release("me/fw", board="cores3", version=None, cache=cache, fetch=fetch)
    assert first == second == cache / "muse-gadget-selfhost-cores3-0.2.0.zip"
    assert first.read_bytes() == zip_bytes
    assert calls == [
        "https://api.github.com/repos/me/fw/releases?per_page=100",
        "https://example/selfhost-v0.2.0/cores3.zip",
        "https://api.github.com/repos/me/fw/releases?per_page=100",
    ]


# -- finding the gadget ------------------------------------------------------------------


def port(device, vid=0x303A, desc="USB JTAG/serial debug unit"):
    return SimpleNamespace(device=device, vid=vid, description=desc)


def test_the_one_espressif_port_is_used():
    ports = [port("/dev/ttyAMA0", vid=None), port("/dev/ttyACM0")]
    assert flash.find_port(None, list_ports=lambda: ports) == "/dev/ttyACM0"


def test_no_gadget_or_several_need_a_port():
    with pytest.raises(flash.FlashError, match="plug"):
        flash.find_port(None, list_ports=lambda: [port("/dev/ttyAMA0", vid=None)])
    with pytest.raises(flash.FlashError, match="--port.*ttyACM0.*ttyACM1"):
        flash.find_port(None, list_ports=lambda: [port("/dev/ttyACM0"), port("/dev/ttyACM1")])


def test_a_given_port_is_used_as_is():
    assert flash.find_port("/dev/ttyUSB3", list_ports=lambda: []) == "/dev/ttyUSB3"


# -- writing -----------------------------------------------------------------------------


class FakeEsptool:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, args):
        self.calls.append(list(args))
        return 1 if self.fail_on and self.fail_on in args else 0


def written(fake):
    """(command, args after the command) per esptool call, with temp paths reduced to names."""
    out = []
    for call in fake.calls:
        cmd = next(a for a in call if a in ("write-flash", "erase-region"))
        rest = [a.rsplit("/", 1)[-1] for a in call[call.index(cmd) + 1 :]]
        out.append((cmd, call[: call.index(cmd)], rest))
    return out


def test_images_are_written_at_their_offsets_and_settings_are_kept(tmp_path):
    fw = flash.load_firmware(make_zip(tmp_path / "fw.zip"), board="cores3")
    fake = FakeEsptool()
    flash.write(fw, "/dev/ttyACM0", erase_settings=False, esptool=fake)
    [(cmd, before, rest)] = written(fake)
    assert cmd == "write-flash"
    assert before == [
        "--chip",
        "esp32s3",
        "--port",
        "/dev/ttyACM0",
        "--baud",
        "460800",
        "--before",
        "default-reset",
        "--after",
        "hard-reset",
    ]
    assert rest == [
        "--flash-mode",
        "dio",
        "--flash-freq",
        "80m",
        "--flash-size",
        "keep",
        "0x0",
        "bootloader.bin",
        "0x10000",
        "partition-table.bin",
        "0x1d000",
        "ota_data_initial.bin",
        "0x20000",
        "muse-gadget.bin",
    ]


def test_erase_settings_erases_only_nvs_first(tmp_path):
    fw = flash.load_firmware(make_zip(tmp_path / "fw.zip"), board="cores3")
    fake = FakeEsptool()
    flash.write(fw, "/dev/ttyACM0", erase_settings=True, esptool=fake)
    (erase, ebefore, erest), (write, _, _) = written(fake)
    assert (erase, erest) == ("erase-region", ["0x11000", "0xc000"])
    assert ebefore[-2:] == ["--after", "no-reset"]  # straight on to the write
    assert write == "write-flash"


def test_a_failed_esptool_run_is_an_error_naming_the_step(tmp_path):
    fw = flash.load_firmware(make_zip(tmp_path / "fw.zip"), board="cores3")
    with pytest.raises(flash.FlashError, match="writing"):
        flash.write(fw, "/dev/ttyACM0", erase_settings=False, esptool=FakeEsptool("write-flash"))


def test_erasing_refuses_a_settings_region_over_factory_data(tmp_path):
    table = partition_table()
    # Corrupt the nvs row so it reaches into prod_data.
    bad = table.replace(struct.pack("<LL", *NVS), struct.pack("<LL", 0x11000, 0x810000), 1)
    with pytest.raises(flash.FlashError, match="prod_data"):
        flash.load_firmware(make_zip(tmp_path / "fw.zip", table=bad), board="cores3")


# -- the command -------------------------------------------------------------------------


@pytest.fixture
def fakes(monkeypatch):
    fake = FakeEsptool()
    monkeypatch.setattr(flash, "run_esptool", fake)
    monkeypatch.setattr(flash, "serial_ports", lambda: [port("/dev/ttyACM0")])
    return fake


def test_flash_from_a_file_confirms_then_writes(state, tmp_path, fakes, capsys, monkeypatch):
    from musehost.cli import main

    zip_path = make_zip(tmp_path / "fw.zip")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert main(["--state-dir", str(state), "flash", "--file", str(zip_path)]) == 0
    out = capsys.readouterr().out
    assert "cores3" in out and "0.2.0" in out and "/dev/ttyACM0" in out
    assert "Flashed cores3 0.2.0" in out and "musehost pair" in out
    assert len(fakes.calls) == 1


def test_answering_no_writes_nothing(state, tmp_path, fakes, monkeypatch):
    from musehost.cli import main

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert (
        main(["--state-dir", str(state), "flash", "--file", str(make_zip(tmp_path / "f.zip"))]) != 0
    )
    assert fakes.calls == []


def test_yes_skips_the_question_and_erase_settings_warns(
    state, tmp_path, fakes, capsys, monkeypatch
):
    from musehost.cli import main

    def no_input(prompt=""):
        raise AssertionError("asked")

    monkeypatch.setattr("builtins.input", no_input)
    args = [
        "--state-dir",
        str(state),
        "flash",
        "--file",
        str(make_zip(tmp_path / "f.zip")),
        "--yes",
        "--erase-settings",
    ]
    assert main(args) == 0
    assert "erase" in capsys.readouterr().out.lower()
    assert [c[c.index("--after") + 2] for c in fakes.calls] == ["erase-region", "write-flash"]


def test_a_bad_file_is_refused_before_touching_the_gadget(state, tmp_path, fakes, capsys):
    from musehost.cli import main

    zip_path = make_zip(tmp_path / "f.zip", tamper="bootloader.bin")
    assert main(["--state-dir", str(state), "flash", "--file", str(zip_path), "--yes"]) != 0
    assert "checksum" in capsys.readouterr().err
    assert fakes.calls == []


def test_the_release_comes_from_the_configured_repo(state, tmp_path, fakes, monkeypatch, capsys):
    import dataclasses

    from musehost.cli import main
    from musehost.config import HostConfig

    config = HostConfig.load(state / "host.toml")
    assert config.firmware_repo == "jksim/muse-gadget-sdk-selfhost"
    dataclasses.replace(config, firmware_repo="me/fw").save(state / "host.toml")
    zip_bytes = make_zip(tmp_path / "src.zip").read_bytes()
    seen = []

    def fetch(url):
        seen.append(url)
        return (
            json.dumps(releases("selfhost-v0.2.0")).encode() if "api.github" in url else zip_bytes
        )

    monkeypatch.setattr(flash, "http_get", fetch)
    assert main(["--state-dir", str(state), "flash", "--yes"]) == 0
    assert seen[0] == "https://api.github.com/repos/me/fw/releases?per_page=100"
    assert (state / "firmware" / "muse-gadget-selfhost-cores3-0.2.0.zip").exists()


def test_http_get_follows_github_redirects(monkeypatch):
    """The real fetcher: a plain urllib GET with a User-Agent, body returned as bytes."""
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        seen["ua"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return Response(b"body")

    monkeypatch.setattr(flash.urllib.request, "urlopen", urlopen)
    assert flash.http_get("https://api.github.com/x") == b"body"
    assert seen["ua"].startswith("musehost") and seen["timeout"] > 0
    with pytest.raises(flash.FlashError, match="https"):
        flash.http_get("file:///etc/passwd")


def test_esptool_runs_in_an_empty_directory_not_the_callers(monkeypatch, tmp_path):
    """esptool reads esptool.cfg from its working directory; never the caller's."""
    import os

    (tmp_path / "esptool.cfg").write_text("[esptool]\n")
    monkeypatch.chdir(tmp_path)
    seen = {}

    def run(cmd, check, cwd):
        seen["cmd"], seen["cwd"], seen["files"] = cmd, cwd, os.listdir(cwd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(flash.subprocess, "run", run)
    assert flash.run_esptool(["version"]) == 0
    assert seen["cmd"][1:] == ["-m", "esptool", "version"]
    assert seen["cwd"] != str(tmp_path) and seen["files"] == []
