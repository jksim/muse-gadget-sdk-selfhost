"""The phone side of pairing v5 (musehost.pair) against published vectors and
the SDK's own device-side implementation."""

import asyncio
import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from musegadget.ble_setup import SetupController
from musegadget.identity import Identity
from musegadget.pairing import PairingSession

from musehost import pair

VECTORS = json.loads(
    (Path(__file__).parents[2] / "linux/tests/vectors/link_pairing_v5.json").read_text()
)["vectors"]


def vector(name: str) -> dict:
    return next(v for v in VECTORS if v["name"] == name)


def b64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def private_key(hex_scalar: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int(hex_scalar, 16), ec.SECP256R1())


def device_info_from(v: dict) -> dict:
    return {
        "type": "device_info",
        "node_id": v["node_id"],
        "device_id": v["device_id"],
        "mac": v["mac"],
        "model": v["model"],
        "version": v["firmware_version"],
        "pairing_protocol": v["version"],
        "pairing_auth": v["pairing_auth"],
        "pairing_auth_epoch": v["pairing_auth_epoch"],
        "pairing_policy": v["pairing_policy"],
    }


def ready_from(v: dict) -> dict:
    return {
        "type": "pairing_ready",
        "version": v["version"],
        "device_id": v["device_id"],
        "node_id": v["node_id"],
        "mac": v["mac"],
        "model": v["model"],
        "firmware_version": v["firmware_version"],
        "pairing_auth": v["pairing_auth"],
        "pairing_auth_epoch": v["pairing_auth_epoch"],
        "pairing_policy": v["pairing_policy"],
        "device_pub": v["device_pub"],
        "device_nonce": v["device_nonce"],
        "transcript_hash": v["transcript_hash"],
        "session_id": v["session_id"],
    }


# -- Published vectors ---------------------------------------------------------


@pytest.mark.parametrize("name", ["community_v5", "community_app_v5"])
def test_the_hello_matches_the_vector(name):
    v = vector(name)
    hello = pair.client_hello(
        device_info_from(v), private_key(v["mobile_private_scalar_hex"]), b64(v["mobile_nonce"])
    )
    assert hello == {
        "action": "pairing_client_hello",
        "version": 5,
        "pairing_auth": "none",
        "pairing_policy": v["pairing_policy"],
        "mobile_pub": v["mobile_pub"],
        "mobile_nonce": v["mobile_nonce"],
    }


@pytest.mark.parametrize("name", ["community_v5", "community_app_v5"])
def test_the_session_and_client_finished_record_match_the_vector(name):
    v = vector(name)
    session = pair.Session.establish(
        device_info_from(v),
        ready_from(v),
        private_key(v["mobile_private_scalar_hex"]),
        b64(v["mobile_nonce"]),
    )
    assert session.session_id == v["session_id"]
    record = session.seal({"action": "pairing_client_finished"})
    assert record == {
        "action": "pairing_encrypted",
        "session_id": v["session_id"],
        "counter": "0",
        "ciphertext": v["client_finished_ciphertext"],
        "tag": v["client_finished_tag"],
    }


def test_a_ready_whose_transcript_hash_disagrees_is_refused():
    v = vector("community_v5")
    ready = {**ready_from(v), "transcript_hash": vector("community_app_v5")["transcript_hash"]}
    with pytest.raises(pair.PairingFailed, match="transcript"):
        pair.Session.establish(
            device_info_from(v),
            ready,
            private_key(v["mobile_private_scalar_hex"]),
            b64(v["mobile_nonce"]),
        )


def test_a_ready_for_another_device_is_refused():
    v = vector("community_v5")
    with pytest.raises(pair.PairingFailed, match="node"):
        pair.Session.establish(
            {**device_info_from(v), "node_id": "homelink-ffffff"},
            ready_from(v),
            private_key(v["mobile_private_scalar_hex"]),
            b64(v["mobile_nonce"]),
        )


def test_official_pairing_is_not_supported():
    v = vector("official_v5")
    with pytest.raises(pair.PairingFailed, match="community"):
        pair.client_hello(device_info_from(v), private_key(v["mobile_private_scalar_hex"]), b"")


def test_records_from_the_device_must_arrive_in_order():
    v = vector("community_app_v5")
    session = pair.Session.establish(
        device_info_from(v),
        ready_from(v),
        private_key(v["mobile_private_scalar_hex"]),
        b64(v["mobile_nonce"]),
    )
    device = PairingSession(
        node_id=v["node_id"],
        device_id=v["device_id"],
        mac=v["mac"],
        firmware_version=v["firmware_version"],
        generate_key=lambda: private_key(v["device_private_scalar_hex"]),
        random_bytes=lambda n: b64(v["device_nonce"]),
    )
    device.handle_hello(
        pair.client_hello(
            device_info_from(v), private_key(v["mobile_private_scalar_hex"]), b64(v["mobile_nonce"])
        )
    )
    first = device.encrypt_json('{"type":"status","status":"one"}')
    second = device.encrypt_json('{"type":"status","status":"two"}')
    with pytest.raises(pair.PairingFailed):
        session.open(second)  # skipped counter 0
    tampered = {**first, "tag": first["tag"][:-2] + ("AA" if first["tag"][-2:] != "AA" else "BB")}
    with pytest.raises(pair.PairingFailed):
        session.open(tampered)


# -- In memory against the SDK's device-side controller --------------------------


class FakeNetwork:
    def is_online(self) -> bool:
        return True

    def current_connection_entry(self) -> dict:
        return {"ssid": "HomeNet", "rssi": -40, "secure": True}


class MemoryLink:
    """BLE between musehost.pair and the SDK's SetupController, in memory.

    The device is the Linux SDK's implementation, which confirms with
    ``confirm_app``; the ESP32's ``confirm_press`` differs only in the policy
    string (covered by the vectors) and in when ``pairing_confirmed`` arrives.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, mtu: int = 185) -> None:
        self.mtu = mtu
        self._loop = loop
        self._notifications: asyncio.Queue[bytes] = asyncio.Queue()
        self.disconnects: list[float] = []
        self.saved: list = []
        self.provision_outcome = None  # None: commit; else a ProvisionFailed status
        self.device = PairingSession(
            node_id="homelink-abcdef",
            device_id="hatch-link:02:00:00:ab:cd:ef",
            mac="02:00:00:ab:cd:ef",
            firmware_version="1.2.3",
        )
        self.controller = SetupController(
            pairing=self.device,
            identity=Identity("02:00:00:ab:cd:ef"),
            version="1.2.3",
            transport=self,
            network=FakeNetwork(),
            provision=self._provision,
        )
        self.controller.start()
        self.drop_after_client_finished = False
        self._writes = 0

    # Transport the SetupController sends through (device -> phone).
    def send_packets(self, packets: list[bytes]) -> None:
        for packet in packets:
            self._loop.call_soon_threadsafe(self._notifications.put_nowait, packet)

    def disconnect(self, delay: float) -> None:
        self.disconnects.append(delay)

    def _provision(self, credentials, commit) -> None:
        from musegadget.ble_setup import ProvisionFailed

        if self.provision_outcome:
            raise ProvisionFailed(self.provision_outcome)
        if not commit(lambda: self.saved.append(credentials) or True):
            raise ProvisionFailed("error_storage")

    # musehost.pair's view (phone -> device).
    async def write(self, packet: bytes) -> None:
        self.controller.on_write(packet)

    async def read(self) -> bytes:
        packet = await self._notifications.get()
        if self.drop_after_client_finished and self.device.confirmed:
            await asyncio.Event().wait()  # the confirmation never arrives
        return packet


def run(coro):
    return asyncio.run(coro)


def make_link(**kwargs) -> MemoryLink:
    link = MemoryLink(asyncio.get_running_loop(), **kwargs)
    link.controller._transport = _ControllerTransport(link)
    return link


class _ControllerTransport:
    def __init__(self, link: MemoryLink) -> None:
        self._link = link

    def send_packets(self, packets):
        self._link.send_packets(packets)

    def mtu(self) -> int:
        return self._link.mtu

    def disconnect(self, delay: float) -> None:
        self._link.disconnect(delay)


def test_pairing_with_the_sdk_device_confirms_and_records_flow_both_ways():
    async def scenario():
        link = make_link()
        client = pair.PairingClient(link)
        info = await client.pair(confirm_timeout=5)
        assert info["node_id"] == "homelink-abcdef"
        assert link.device.confirmed
        await client.send_encrypted({"action": "wifi_scan"})
        scan = await client.receive_encrypted(timeout=5)
        assert scan["type"] == "wifi_scan_result"

    run(scenario())


def test_pairing_works_at_the_minimum_mtu():
    async def scenario():
        client = pair.PairingClient(make_link(mtu=23))
        assert (await client.pair(confirm_timeout=5))["node_id"] == "homelink-abcdef"

    run(scenario())


def test_without_a_confirmation_pairing_times_out():
    async def scenario():
        link = make_link()
        link.drop_after_client_finished = True
        with pytest.raises(pair.PairingFailed, match="confirm"):
            await pair.PairingClient(link).pair(confirm_timeout=0.3)

    run(scenario())


def test_a_confirm_timeout_status_from_the_device_fails_pairing():
    async def scenario():
        link = make_link()
        original = link.device.encrypt_status

        def timed_out(status, generation=0):
            if status == "pairing_confirmed":
                status = "pairing_confirm_timeout"
            return original(status, generation)

        link.device.encrypt_status = timed_out
        with pytest.raises(pair.PairingFailed, match="pairing_confirm_timeout"):
            await pair.PairingClient(link).pair(confirm_timeout=5)

    run(scenario())


# -- Provisioning ------------------------------------------------------------------

from musehost.config import HostConfig  # noqa: E402
from musehost.store import Store  # noqa: E402
from musehost.tokens import Tokens  # noqa: E402

CONFIG = HostConfig(hostnames=("muse-host.local",), ips=("192.168.4.218",), port=443)


def _host_trust() -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import x25519

    from musehost import pki

    _, ca = pki.create_ca()
    return {
        "ca_pem": ca.public_bytes(serialization.Encoding.PEM).decode(),
        "noise_static_pub": pki.noise_public_b64(x25519.X25519PrivateKey.generate()),
    }


TRUST = _host_trust()


def make_tokens(tmp_path) -> Tokens:
    return Tokens(Store.open(tmp_path / "musehost.db"))


async def paired(link) -> tuple[pair.PairingClient, dict]:
    client = pair.PairingClient(link)
    return client, await client.pair(confirm_timeout=5)


def test_provisioning_sends_wifi_tokens_and_this_host(tmp_path):
    tokens = make_tokens(tmp_path)

    async def scenario():
        link = make_link()
        client, info = await paired(link)
        node_id = await pair.provision(
            client,
            info,
            tokens=tokens,
            config=CONFIG,
            ssid="HomeNet",
            password="hunter22",
            **TRUST,
        )
        return link, node_id

    link, node_id = run(scenario())
    assert node_id == "homelink-abcdef"
    [saved] = link.saved
    assert (saved.api_url_v2, saved.noise_host) == ("https://muse-host.local", "muse-host.local")
    # One firmware for every host: it learns the host's CA and Noise key here.
    assert (saved.ca_cert, saved.noise_static_pub) == (TRUST["ca_pem"], TRUST["noise_static_pub"])
    assert tokens.device_for_access(saved.access_token) == "homelink-abcdef"
    assert tokens.refresh(saved.refresh_token, "homelink-abcdef") is not None


@pytest.mark.parametrize("outcome", ["auth_failed", "error_storage"])
def test_a_failed_provision_names_the_status_and_revokes_the_tokens(tmp_path, outcome):
    tokens = make_tokens(tmp_path)

    async def scenario():
        link = make_link()
        link.provision_outcome = outcome
        client, info = await paired(link)
        with pytest.raises(pair.PairingFailed, match=outcome):
            await pair.provision(
                client,
                info,
                tokens=tokens,
                config=CONFIG,
                ssid="HomeNet",
                password="hunter22",
                **TRUST,
            )

    run(scenario())
    row = tokens.store.db.execute(
        "SELECT revoked_at FROM devices WHERE node_id = 'homelink-abcdef'"
    ).fetchone()
    assert row["revoked_at"] is not None


@pytest.mark.parametrize(
    "field, value, status",
    [
        ("ca_pem", "not a certificate", "error_invalid_ca"),
        ("noise_static_pub", "too-short", "error_invalid_noise_key"),
    ],
)
def test_unusable_trust_fails_pairing_and_revokes_the_tokens(tmp_path, field, value, status):
    tokens = make_tokens(tmp_path)

    async def scenario():
        client, info = await paired(make_link())
        with pytest.raises(pair.PairingFailed, match=status):
            await pair.provision(
                client,
                info,
                tokens=tokens,
                config=CONFIG,
                ssid="HomeNet",
                password="hunter22",
                **{**TRUST, field: value},
            )

    run(scenario())
    assert tokens.store.devices()[0]["revoked_at"] is not None


def test_wifi_failure_is_reported_and_revokes_the_tokens(tmp_path):
    tokens = make_tokens(tmp_path)

    async def scenario():
        link = make_link()
        link.controller._network.is_online = lambda: False
        client, info = await paired(link)
        with pytest.raises(pair.PairingFailed, match="wifi_failed"):
            await pair.provision(
                client,
                info,
                tokens=tokens,
                config=CONFIG,
                ssid="HomeNet",
                password="hunter22",
                **TRUST,
            )

    run(scenario())
    assert tokens.store.devices()[0]["revoked_at"] is not None


def test_an_empty_wifi_password_is_refused_before_anything_is_sent(tmp_path):
    tokens = make_tokens(tmp_path)

    async def scenario():
        client, info = await paired(make_link())
        with pytest.raises(pair.PairingFailed, match="password"):
            await pair.provision(
                client,
                info,
                tokens=tokens,
                config=CONFIG,
                ssid="HomeNet",
                password="",
                **TRUST,
            )

    run(scenario())
    assert tokens.store.devices() == []


def test_provisioning_logs_no_secrets(tmp_path, caplog, capsys):
    import logging

    caplog.set_level(logging.DEBUG)
    tokens = make_tokens(tmp_path)

    async def scenario():
        link = make_link()
        client, info = await paired(link)
        await pair.provision(
            client,
            info,
            tokens=tokens,
            config=CONFIG,
            ssid="HomeNet",
            password="hunter22",
            **TRUST,
        )
        return link.saved[0]

    saved = run(scenario())
    out = caplog.text + "".join(capsys.readouterr())
    for secret in ("hunter22", saved.access_token, saved.refresh_token):
        assert secret not in out
    # Public, but long and of no use in a log.
    assert TRUST["noise_static_pub"] not in out and "BEGIN CERTIFICATE" not in out
