"""The phone side of gadget pairing v5, as the Muse app does it.

Community pairing only (``pairing_auth: "none"``): the device generates an
ephemeral P-256 key per attempt, both sides derive AES-GCM record keys from the
ECDH secret and a shared transcript, and the owner confirms on the device (a
button press for ``confirm_press``, the ESP32's policy). The transcript, key
schedule and record format come from the SDK's ``musegadget.pairing`` so they
can't drift from the device side.

Community pairing keeps setup secrets from passive listeners; it does not
authenticate the device, so an active attacker in radio range during the
confirm window could sit in the middle. Pair close to the device.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from musegadget.ble_framing import ChunkAssembler, encode_chunks
from musegadget.pairing import (
    AUTH_COMMUNITY,
    PAIRING_VERSION,
    b64url_decode,
    b64url_encode,
    build_transcript,
    derive_session_keys,
    parse_counter,
    record_aad,
    record_nonce,
)

if TYPE_CHECKING:
    from musehost.config import HostConfig
    from musehost.tokens import Tokens

log = logging.getLogger(__name__)

TO_DEVICE, FROM_DEVICE = 0, 1  # record directions, as in musegadget.pairing
NONCE_BYTES = 16
TAG_BYTES = 16
DEFAULT_TIMEOUT_S = 15
CONFIRM_TIMEOUT_S = 75  # the device's own window is 60 s
PROVISION_TIMEOUT_S = 120
FAILED_STATUSES = ("pairing_confirm_timeout", "wifi_failed", "auth_failed", "vm_failed")


class PairingFailed(Exception):
    """Pairing or provisioning stopped; the message names the step."""


class Link(Protocol):
    """A BLE connection to the gadget's setup service."""

    mtu: int

    async def write(self, packet: bytes) -> None: ...

    async def read(self) -> bytes:
        """The next TX notification."""
        ...


def client_hello(device_info: dict, key: ec.EllipticCurvePrivateKey, nonce: bytes) -> dict:
    if device_info.get("pairing_protocol") != PAIRING_VERSION:
        raise PairingFailed(f"device speaks pairing v{device_info.get('pairing_protocol')}")
    if device_info.get("pairing_auth") != AUTH_COMMUNITY:
        raise PairingFailed("only community pairing is supported")
    return {
        "action": "pairing_client_hello",
        "version": PAIRING_VERSION,
        "pairing_auth": AUTH_COMMUNITY,
        "pairing_policy": device_info.get("pairing_policy"),
        "mobile_pub": b64url_encode(_public_bytes(key)),
        "mobile_nonce": b64url_encode(nonce),
    }


class Session:
    """Record keys for one confirmed-or-confirming pairing attempt."""

    def __init__(self, tx_key: bytes, rx_key: bytes, session_id: str) -> None:
        self._tx = AESGCM(tx_key)
        self._rx = AESGCM(rx_key)
        self.session_id = session_id
        self._tx_counter = 0
        self._rx_counter = 0

    @classmethod
    def establish(
        cls,
        device_info: dict,
        ready: dict,
        key: ec.EllipticCurvePrivateKey,
        nonce: bytes,
    ) -> Session:
        """Check ``pairing_ready`` against our own transcript and derive keys."""
        if ready.get("node_id") != device_info.get("node_id"):
            raise PairingFailed("pairing_ready is for another node")
        try:
            device_pub = b64url_decode(ready.get("device_pub"))
            device_nonce = b64url_decode(ready.get("device_nonce"))
            peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), device_pub)
            transcript = build_transcript(
                community=True,
                auth_epoch=int(ready.get("pairing_auth_epoch", -1)),
                policy=device_info.get("pairing_policy"),
                device_id=ready.get("device_id") or "",
                node_id=ready.get("node_id") or "",
                mac=ready.get("mac") or "",
                firmware_version=ready.get("firmware_version") or "",
                mobile_pub=b64url_encode(_public_bytes(key)),
                device_pub=b64url_encode(device_pub),
                mobile_nonce=b64url_encode(nonce),
                device_nonce=b64url_encode(device_nonce),
            )
        except (ValueError, TypeError) as exc:
            raise PairingFailed(f"invalid pairing_ready: {exc}") from None
        transcript_hash = hashlib.sha256(transcript.encode()).digest()
        if b64url_encode(transcript_hash) != ready.get("transcript_hash"):
            raise PairingFailed("pairing_ready transcript hash does not match ours")
        secret = key.exchange(ec.ECDH(), peer)
        tx_key, rx_key, session_id = derive_session_keys(
            secret, nonce, device_nonce, transcript_hash
        )
        session = cls(tx_key, rx_key, b64url_encode(session_id))
        if session.session_id != ready.get("session_id"):
            raise PairingFailed("pairing_ready session id does not match ours")
        return session

    def seal(self, command: dict) -> dict:
        counter = self._tx_counter
        sealed = self._tx.encrypt(
            record_nonce(TO_DEVICE, counter),
            json.dumps(command, separators=(",", ":")).encode(),
            record_aad(self.session_id, TO_DEVICE, counter),
        )
        self._tx_counter += 1
        return {
            "action": "pairing_encrypted",
            "session_id": self.session_id,
            "counter": str(counter),
            "ciphertext": b64url_encode(sealed[:-TAG_BYTES]),
            "tag": b64url_encode(sealed[-TAG_BYTES:]),
        }

    def open(self, envelope: dict) -> dict:
        try:
            if envelope.get("session_id") != self.session_id:
                raise ValueError("wrong session")
            counter = parse_counter(envelope.get("counter"))
            if counter != self._rx_counter:
                raise ValueError(f"counter {counter}, expected {self._rx_counter}")
            plaintext = self._rx.decrypt(
                record_nonce(FROM_DEVICE, counter),
                b64url_decode(envelope.get("ciphertext"), 16384)
                + b64url_decode(envelope.get("tag")),
                record_aad(self.session_id, FROM_DEVICE, counter),
            )
            message = json.loads(plaintext)
        except (ValueError, InvalidTag) as exc:
            raise PairingFailed(f"unreadable record from the device: {exc!r}") from None
        self._rx_counter += 1
        if not isinstance(message, dict):
            raise PairingFailed("record from the device is not a JSON object")
        return message


class PairingClient:
    """Pairs with one gadget over a :class:`Link`."""

    def __init__(self, link: Link) -> None:
        self._link = link
        self._assembler = ChunkAssembler()
        self._session: Session | None = None

    async def send(self, message: dict) -> None:
        data = json.dumps(message, separators=(",", ":")).encode()
        for packet in encode_chunks(data, self._link.mtu):
            await self._link.write(packet)

    async def receive(self, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                raw = self._assembler.feed(await self._link.read())
                if raw is None:
                    continue
                try:
                    message = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    log.debug("ignoring a non-JSON notification")
                    continue
                if isinstance(message, dict):
                    return message

    async def get_device_info(self) -> dict:
        await self.send({"action": "get_device_info"})
        while True:
            message = await self.receive()
            if message.get("type") == "device_info":
                return message

    async def pair(
        self,
        confirm_timeout: float = CONFIRM_TIMEOUT_S,
        on_confirm_wait: Callable[[dict], None] | None = None,
    ) -> dict:
        """Run the handshake until the owner confirms; returns the device info.

        ``on_confirm_wait`` runs once the device is waiting for the owner.
        """
        info = await self.get_device_info()
        key = ec.generate_private_key(ec.SECP256R1())
        nonce = os.urandom(NONCE_BYTES)
        await self.send(client_hello(info, key, nonce))
        ready = await self._expect_plaintext("pairing_ready")
        self._session = Session.establish(info, ready, key, nonce)
        await self.send_encrypted({"action": "pairing_client_finished"})
        if on_confirm_wait is not None:
            on_confirm_wait(info)
        try:
            async with asyncio.timeout(confirm_timeout):
                while True:
                    status = (await self.receive_encrypted(timeout=confirm_timeout)).get("status")
                    if status == "pairing_confirmed":
                        return info
                    if status in FAILED_STATUSES or str(status).startswith("error_"):
                        raise PairingFailed(f"device refused pairing: {status}")
        except TimeoutError:
            raise PairingFailed("no confirmation from the device in time") from None

    async def send_encrypted(self, command: dict) -> None:
        if self._session is None:
            raise PairingFailed("no pairing session")
        await self.send(self._session.seal(command))

    async def receive_encrypted(self, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
        """The next record from the device; plaintext errors end the session."""
        if self._session is None:
            raise PairingFailed("no pairing session")
        while True:
            message = await self.receive(timeout)
            if message.get("type") == "pairing_encrypted":
                return self._session.open(message)
            if message.get("type") == "status":
                raise PairingFailed(f"device reported {message.get('status')}")

    async def _expect_plaintext(self, kind: str) -> dict:
        message = await self.receive()
        if message.get("type") == kind:
            return message
        if message.get("type") == "status":
            raise PairingFailed(f"device reported {message.get('status')}")
        raise PairingFailed(f"expected {kind}, got {message.get('type')!r}")


async def provision(
    client: PairingClient,
    device_info: dict,
    *,
    tokens: Tokens,
    config: HostConfig,
    ssid: str,
    password: str,
    ca_pem: str,
    noise_static_pub: str,
    display_name: str = "",
    timeout: float = PROVISION_TIMEOUT_S,
) -> str:
    """Enroll a confirmed gadget and hand it Wi-Fi, tokens and this host.

    "This host" is its addresses plus its CA (``ca_pem``) and Noise public key
    (``noise_static_pub``, unpadded base64url), so one gadget firmware works
    with any host: it trusts that CA for this host and pins that key.

    Returns its node id once it reports ``auth_ok``. Boards that restart after
    pairing (those with a full UI) send ``auth_ok`` once the credentials are
    stored and reach the host only after restarting. Any failure revokes the
    tokens just issued, so none stay usable.
    """
    if not ssid or not password:
        # The ESP32 firmware refuses open networks.
        raise PairingFailed("Wi-Fi SSID and password are both required")
    node_id = device_info["node_id"]
    access, refresh = tokens.enroll(node_id, display_name or node_id)
    log.info("enrolled %s; sending Wi-Fi and host details", node_id)
    try:
        await client.send_encrypted(
            {
                "action": "provision_v2",
                "ssid": ssid,
                "password": password,
                "access_token": access,
                "refresh_token": refresh,
                "token_type": "device",
                "api_url_v2": config.api_url,
                "noise_host": config.public_host,
                "ca_cert": ca_pem,
                "noise_static_pub": noise_static_pub,
            }
        )
        async with asyncio.timeout(timeout):
            while True:
                status = (await client.receive_encrypted(timeout=timeout)).get("status")
                log.info("device: %s", status)
                if status == "auth_ok":
                    return node_id
                if status in FAILED_STATUSES or str(status).startswith("error_"):
                    raise PairingFailed(f"provisioning failed: {status}")
    except BaseException as exc:
        tokens.revoke(node_id)
        if isinstance(exc, TimeoutError):
            raise PairingFailed("no auth_ok from the device in time") from None
        raise


async def run_pairing(
    state: Path,
    gadget,
    *,
    ssid: str,
    password: str,
    display_name: str = "",
    wait_s: float = 60,
    progress=print,
) -> tuple[str, bool]:
    """Pair ``gadget`` (a ``ble_client.Gadget``) over Bluetooth and provision it.

    Shared by ``musehost pair`` and the dashboard's pair job; ``progress`` gets
    each step as a line of text, never the password. Returns the node id and
    whether the gadget reached the host within ``wait_s``. Raises
    PairingFailed; tokens issued for a failed or cancelled pairing are revoked.
    """
    from musehost import ble_client, pki
    from musehost.config import HostConfig
    from musehost.store import Store
    from musehost.tokens import Tokens

    config = HostConfig.load(state / "host.toml")
    tokens = Tokens(Store.open(state / "musehost.db"))
    started = tokens.now()
    progress(f"Connecting to {gadget.name}...")
    async with ble_client.BleLink(gadget.address) as link:
        client = PairingClient(link)
        info = await client.pair(
            on_confirm_wait=lambda info: progress(
                f"Press the button on {gadget.name} to confirm (60 s)..."
            )
        )
        progress(f"Confirmed: {info['node_id']}. Sending Wi-Fi and host details...")
        node_id = await provision(
            client,
            info,
            tokens=tokens,
            config=config,
            ssid=ssid,
            password=password,
            ca_pem=(state / "ca.pem").read_text(),
            noise_static_pub=pki.noise_public_b64(pki.load_noise_key(state / "noise_static.key")),
            display_name=display_name or gadget.name,
        )
    progress(f"Provisioned {node_id}. Waiting for it to reach the host...")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        row = tokens.store.db.execute(
            "SELECT last_seen FROM devices WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row and row["last_seen"] and row["last_seen"] >= started:
            progress(f"{node_id} reached the host.")
            return node_id, True
        await asyncio.sleep(1)
    progress(f"{node_id} hasn't reached the host yet; check its log or `musehost devices list`.")
    return node_id, False


def _public_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
