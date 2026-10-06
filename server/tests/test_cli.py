import stat
import tomllib

import pytest

from musehost.cli import main

STATE_FILES = [
    "host.toml",
    "ca.pem",
    "ca.key",
    "server.pem",
    "server.key",
    "noise_static.key",
    "musehost.db",
]
SECRET_FILES = ["ca.key", "server.key", "noise_static.key", "musehost.db"]


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_init_writes_every_state_file_with_private_modes(tmp_path):
    state = tmp_path / "state"
    assert (
        main(
            [
                "--state-dir",
                str(state),
                "init",
                "--hostname",
                "musehost.local",
                "--hostname",
                "musehost.lan",
                "--ip",
                "192.168.1.10",
                "--port",
                "9443",
            ]
        )
        == 0
    )
    assert sorted(p.name for p in state.iterdir()) == sorted(STATE_FILES)
    assert mode(state) == 0o700
    for name in SECRET_FILES:
        assert mode(state / name) == 0o600, name


def test_init_records_names_port_and_the_provisioned_address(tmp_path):
    main(
        [
            "--state-dir",
            str(tmp_path),
            "init",
            "--hostname",
            "musehost.local",
            "--hostname",
            "musehost.lan",
            "--ip",
            "192.168.1.10",
            "--port",
            "9443",
        ]
    )
    config = tomllib.loads((tmp_path / "host.toml").read_text())
    assert config == {
        "hostnames": ["musehost.local", "musehost.lan"],
        "ips": ["192.168.1.10"],
        "port": 9443,
        "vm_id": "home",
        "agent_name": "Clio",
        "speech_model": "base.en",
        "speech_timeout_s": 30.0,
        "brain_provider": "claude",
        "brain_model": "",
        "brain_base_url": "",
        "brain_effort": "low",
        "brain_web_search": True,
        "brain_tools": ["device.health", "display.draw_url", "display.show_animation"],
        "brain_idle_minutes": 30,
        "brain_max_tokens": 4096,
        "tts_voice": "en_US-lessac-medium",
        "firmware_repo": "jksim/muse-gadget-sdk-selfhost",
    }


def test_init_defaults_to_port_8443(tmp_path):
    main(["--state-dir", str(tmp_path), "init", "--hostname", "musehost.local"])
    assert tomllib.loads((tmp_path / "host.toml").read_text())["port"] == 8443


def test_init_needs_a_hostname_or_an_ip(tmp_path, capsys):
    assert main(["--state-dir", str(tmp_path), "init"]) != 0
    assert "--hostname or --ip" in capsys.readouterr().err


def test_init_refuses_to_overwrite_without_force(tmp_path, capsys):
    args = ["--state-dir", str(tmp_path), "init", "--hostname", "musehost.local"]
    assert main(args) == 0
    ca_before = (tmp_path / "ca.pem").read_text()
    assert main(args) != 0
    assert "--force" in capsys.readouterr().err
    assert (tmp_path / "ca.pem").read_text() == ca_before
    assert main([*args, "--force"]) == 0
    assert (tmp_path / "ca.pem").read_text() != ca_before


@pytest.mark.parametrize("ip", ["not-an-ip", "300.1.1.1"])
def test_init_rejects_a_bad_ip(tmp_path, ip):
    assert main(["--state-dir", str(tmp_path), "init", "--ip", ip]) != 0


def test_enroll_writes_a_pairing_file_the_sdk_can_load(state):
    import json

    from musehost import pki
    from musehost.config import HostConfig

    out = state.parent / "pairing.json"
    assert main(["--state-dir", str(state), "enroll", "homelink-abcdef", "--out", str(out)]) == 0
    pairing = json.loads(out.read_text())
    config = HostConfig.load(state / "host.toml")
    assert mode(out) == 0o600
    assert pairing["token_type"] == "device"
    assert pairing["access_token"] and pairing["refresh_token"]
    assert pairing["api_url_v2"] == config.api_url
    assert pairing["noise_host"] == config.public_host
    assert pairing["ca_cert"] == (state / "ca.pem").read_text()
    assert pairing["noise_static_pub"] == pki.noise_public_b64(
        pki.load_noise_key(state / "noise_static.key")
    )
    assert isinstance(pairing["access_token_saved_at"], int)


def test_enroll_rejects_a_malformed_node_id(state, capsys):
    out = state.parent / "pairing.json"
    assert main(["--state-dir", str(state), "enroll", "pi", "--out", str(out)]) != 0
    assert not out.exists()


def enroll(state, node_id: str, name: str = "") -> dict:
    import json

    out = state.parent / f"{node_id}.json"
    args = ["--state-dir", str(state), "enroll", node_id, "--out", str(out)]
    assert main([*args, "--display-name", name] if name else args) == 0
    return json.loads(out.read_text())


def test_devices_list_shows_every_device_and_no_tokens(state, capsys):
    first = enroll(state, "homelink-abcdef", "kitchen pi")
    enroll(state, "homelink-123456")
    capsys.readouterr()
    assert main(["--state-dir", str(state), "devices", "list"]) == 0
    out = capsys.readouterr().out
    assert "homelink-abcdef" in out and "kitchen pi" in out and "homelink-123456" in out
    assert first["access_token"] not in out and first["refresh_token"] not in out


def test_devices_list_marks_revoked_devices(state, capsys):
    enroll(state, "homelink-abcdef")
    main(["--state-dir", str(state), "devices", "revoke", "homelink-abcdef"])
    capsys.readouterr()
    main(["--state-dir", str(state), "devices", "list"])
    assert "revoked" in capsys.readouterr().out


def test_devices_revoke_cuts_off_every_token(state):
    from musehost.store import Store
    from musehost.tokens import Tokens

    pairing = enroll(state, "homelink-abcdef")
    assert main(["--state-dir", str(state), "devices", "revoke", "homelink-abcdef"]) == 0
    tokens = Tokens(Store.open(state / "musehost.db"))
    assert tokens.device_for_access(pairing["access_token"]) is None
    assert tokens.refresh(pairing["refresh_token"], "homelink-abcdef") is None


def test_revoking_an_unknown_device_fails_clearly(state, capsys):
    assert main(["--state-dir", str(state), "devices", "revoke", "homelink-abcdef"]) != 0
    assert "homelink-abcdef" in capsys.readouterr().err


def test_grant_prints_a_code_the_host_url_and_the_ca_fingerprint(state, capsys):
    import subprocess

    from musehost.config import HostConfig
    from musehost.store import Store
    from musehost.tokens import Tokens

    capsys.readouterr()
    assert main(["--state-dir", str(state), "grant"]) == 0
    out = capsys.readouterr().out
    fields = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
    openssl = subprocess.run(
        ["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(state / "ca.pem")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert fields["CA SHA-256"] == openssl.strip().split("=", 1)[1]
    assert fields["Host"] == HostConfig.load(state / "host.toml").api_url
    assert Tokens(Store.open(state / "musehost.db")).redeem_grant(fields["Code"])


# -- musehost pair (with an in-memory gadget instead of the radio) ------------------


class FakeBle:
    """Stands in for ble_client.BleLink, backed by the SDK's device code."""

    reach_host = False
    saved = None  # the credentials the gadget stored

    def __init__(self, address):
        self.address = address

    async def __aenter__(self):
        from test_pair import make_link

        self.link = make_link()
        original = self.link._provision

        def provision(credentials, commit):
            original(credentials, commit)
            FakeBle.saved = credentials
            if FakeBle.reach_host:  # as the gadget's first fetch_vms would
                FakeBle.tokens.device_for_access(credentials.access_token)

        self.link.controller._provision = provision
        return self.link

    async def __aexit__(self, *exc):
        pass


@pytest.fixture
def ble(monkeypatch, state):
    from musehost import ble_client
    from musehost.store import Store
    from musehost.tokens import Tokens

    gadgets = [ble_client.Gadget("MuseGadgetABCDEF", "AA:BB:CC:DD:EE:FF", -50)]

    async def scan(timeout=8.0):
        return list(gadgets)

    monkeypatch.setattr(ble_client, "scan", scan)
    monkeypatch.setattr(ble_client, "BleLink", FakeBle)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "hunter22")
    FakeBle.reach_host = False
    FakeBle.tokens = Tokens(Store.open(state / "musehost.db"))
    return gadgets


def pair_args(state, *extra):
    return ["--state-dir", str(state), "pair", "--ssid", "HomeNet", "--wait", "1", *extra]


def test_pair_provisions_the_only_gadget_and_waits_for_it(state, ble, capsys):
    FakeBle.reach_host = True
    assert main(pair_args(state)) == 0
    out = capsys.readouterr().out
    assert "homelink-abcdef" in out and "reached the host" in out
    assert "hunter22" not in out
    main(["--state-dir", str(state), "devices", "list"])
    assert "homelink-abcdef" in capsys.readouterr().out


def test_pair_hands_the_gadget_this_hosts_ca_and_noise_key(state, ble):
    from musehost import pki

    assert main(pair_args(state)) == 0
    assert FakeBle.saved.ca_cert == (state / "ca.pem").read_text()
    noise_pub = pki.noise_public_b64(pki.load_noise_key(state / "noise_static.key"))
    assert FakeBle.saved.noise_static_pub == noise_pub


def test_pair_warns_when_the_gadget_has_not_reached_the_host_yet(state, ble, capsys):
    assert main(pair_args(state)) == 0
    assert "hasn't reached the host" in capsys.readouterr().out


def test_pair_with_no_gadget_in_setup_mode_fails(state, ble, capsys):
    ble.clear()
    assert main(pair_args(state)) != 0
    assert "no gadget" in capsys.readouterr().err.lower()


def test_pair_with_several_gadgets_needs_a_name(state, ble, capsys):
    from musehost.ble_client import Gadget

    ble.append(Gadget("MuseGadget123456", "11:22:33:44:55:66", -70))
    assert main(pair_args(state)) != 0
    err = capsys.readouterr().err
    assert "MuseGadgetABCDEF" in err and "MuseGadget123456" in err and "--name" in err
    assert main(pair_args(state, "--name", "MuseGadgetABCDEF")) == 0


def test_pair_scan_only_lists_gadgets(state, ble, capsys):
    assert main(["--state-dir", str(state), "pair", "--scan-only"]) == 0
    assert "MuseGadgetABCDEF" in capsys.readouterr().out


def test_pair_reports_a_failed_step_without_secrets(state, ble, capsys, monkeypatch):
    original = FakeBle.__aenter__

    async def failing(self):
        link = await original(self)
        link.provision_outcome = "auth_failed"
        return link

    monkeypatch.setattr(FakeBle, "__aenter__", failing)
    assert main(pair_args(state)) != 0
    captured = capsys.readouterr()
    assert "auth_failed" in captured.err
    assert "hunter22" not in captured.out + captured.err


def test_transcribe_prints_the_text_and_timing(state, tmp_path, capsys, monkeypatch):
    from test_speech import FakeEngine, write_wav

    from musehost import speech

    monkeypatch.setattr(speech, "engine_for", lambda config, models: FakeEngine("hi there"))
    note = write_wav(tmp_path / "n.wav")
    assert main(["--state-dir", str(state), "transcribe", str(note)]) == 0
    out = capsys.readouterr().out
    assert "hi there" in out and " s" in out


def test_transcribe_without_a_model_explains_how_to_get_one(state, tmp_path, capsys):
    from test_speech import write_wav

    note = write_wav(tmp_path / "n.wav")
    assert main(["--state-dir", str(state), "transcribe", str(note)]) != 0
    assert "download-model" in capsys.readouterr().err


def test_say_writes_an_mp3_and_prints_timing(state, tmp_path, capsys, monkeypatch):
    from test_voice_out import FakeVoice, decode

    from musehost import voice_out

    monkeypatch.setattr(voice_out, "engine_for", lambda config, models: FakeVoice())
    out = tmp_path / "clio.mp3"
    assert main(["--state-dir", str(state), "say", "Hello. I'm Clio.", "--out", str(out)]) == 0
    seconds, _, _ = decode(out.read_bytes())
    assert abs(seconds - 1.0) < 0.15
    printed = capsys.readouterr().out
    assert "first sentence" in printed and "audio" in printed


def test_say_without_a_voice_explains_how_to_get_one(state, tmp_path, capsys):
    assert main(["--state-dir", str(state), "say", "Hi.", "--out", str(tmp_path / "x.mp3")]) != 0
    assert "download-voice" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command, module, attr",
    [
        ("download-model", "speech", "download_model"),
        ("download-voice", "voice_out", "download_voice"),
    ],
)
def test_downloads_print_one_line_not_library_chatter(
    state, monkeypatch, capsys, caplog, command, module, attr
):
    import importlib
    import logging
    import warnings

    def noisy_fetch(name, models_dir):
        logging.getLogger("httpx").info('HTTP Request: GET https://huggingface.co/x "200 OK"')
        logging.getLogger("huggingface_hub.utils._http").warning("unauthenticated requests")
        logging.getLogger("piper.download_voices").info("Downloaded: x")
        warnings.warn("`local_dir_use_symlinks` is deprecated", UserWarning, stacklevel=1)
        return models_dir / name

    monkeypatch.setattr(importlib.import_module(f"musehost.{module}"), attr, noisy_fetch)
    caplog.set_level(logging.DEBUG)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert main(["--state-dir", str(state), command]) == 0
    out = capsys.readouterr()
    text = out.out + out.err + caplog.text
    assert "huggingface.co" not in text and "unauthenticated" not in text
    assert "Downloaded:" not in text and not caught
    assert "Downloading" in out.out and " is in " in out.out


def test_quiet_downloads_holds_even_after_the_hub_library_sets_its_own_level():
    """huggingface_hub configures its own logging lazily, on first use, adding a
    handler and resetting its level; quiet must still win. Runs in a fresh
    interpreter so that first use really happens inside the download.
    """
    import subprocess
    import sys

    code = (
        "import logging\n"
        "from musehost.cli import quiet_downloads\n"
        "logging.basicConfig(level=logging.INFO)\n"
        "with quiet_downloads():\n"
        "    from huggingface_hub.utils import logging as hub_logging\n"
        "    hub_logging.get_logger('huggingface_hub.utils._http').warning('HUB-HINT')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "HUB-HINT" not in out.stdout + out.stderr
