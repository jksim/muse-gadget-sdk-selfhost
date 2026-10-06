"""``musehost`` command line."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from musehost import admin, ble_client, flash, pair, pki, provisioning, speech, voice_out
from musehost.app import DB_FILE, create_app
from musehost.config import CONFIG_FILE, HostConfig, state_dir
from musehost.store import Store
from musehost.tokens import Tokens

log = logging.getLogger("musehost")

STATE_FILES = (
    CONFIG_FILE,
    "ca.pem",
    "ca.key",
    "server.pem",
    "server.key",
    "noise_static.key",
    DB_FILE,
)


def _fail(message: str) -> int:
    print(f"musehost: {message}", file=sys.stderr)
    return 1


def cmd_init(args: argparse.Namespace, state: Path) -> int:
    hostnames, ips = args.hostname or [], args.ip or []
    if not hostnames and not ips:
        return _fail("give at least one --hostname or --ip")
    try:
        ips = [str(ipaddress.ip_address(ip)) for ip in ips]
    except ValueError as exc:
        return _fail(str(exc))
    if any((state / name).exists() for name in STATE_FILES) and not args.force:
        return _fail(
            f"{state} already holds host state; pass --force to replace it "
            "(enrolled devices will need enrolling again)"
        )

    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    if args.force:
        (state / DB_FILE).unlink(missing_ok=True)

    config = HostConfig(
        hostnames=tuple(hostnames), ips=tuple(ips), port=args.port, vm_id=args.vm_id
    )
    ca_key, ca = pki.create_ca()
    server_key, server = pki.create_server_cert(ca_key, ca, hostnames, ips)
    pki.write_private_key(state / "ca.key", ca_key)
    pki.write_cert(state / "ca.pem", ca)
    pki.write_private_key(state / "server.key", server_key)
    pki.write_cert(state / "server.pem", server)
    pki.write_private_key(state / "noise_static.key", pki.create_noise_key())
    Store.open(state / DB_FILE).close()
    config.save(state / CONFIG_FILE)

    print(f"Host state written to {state}")
    print(f"Devices will be provisioned with {config.api_url}")
    return 0


def make_server(state: Path, port: int | None = None, bind: str = "0.0.0.0") -> uvicorn.Server:  # noqa: S104
    """A TLS uvicorn server for ``state``; ``port`` overrides host.toml's."""
    config = HostConfig.load(state / CONFIG_FILE)
    if port is not None and port != config.port:
        log.warning(
            "port %d differs from %d in %s; enrolled devices still use %s",
            port,
            config.port,
            CONFIG_FILE,
            config.public_host,
        )
    app = create_app(state)
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host=bind,
            port=config.port if port is None else port,
            ssl_certfile=str(state / "server.pem"),
            ssl_keyfile=str(state / "server.key"),
            log_level="info",
        )
    )


def cmd_serve(args: argparse.Namespace, state: Path) -> int:
    if not (state / CONFIG_FILE).exists():
        return _fail(f"no host state in {state}; run `musehost init` first")
    make_server(state, port=args.port, bind=args.bind).run()
    return 0


def write_private_json(path: Path, data: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def cmd_enroll(args: argparse.Namespace, state: Path) -> int:
    """Enroll a gadget by hand, writing the pairing.json it will load."""
    tokens = Tokens(Store.open(state / DB_FILE))
    try:
        access, refresh = tokens.enroll(args.node_id, args.display_name)
    except ValueError as exc:
        return _fail(str(exc))
    config = HostConfig.load(state / CONFIG_FILE)
    noise_pub = pki.noise_public_b64(pki.load_noise_key(state / "noise_static.key"))
    ca_pem = (state / "ca.pem").read_text()
    pairing = {
        **provisioning.bundle(config, ca_pem, noise_pub, access, refresh),
        "username": "",
        "api_url": "",
        "access_token_saved_at": int(time.time()),
    }
    write_private_json(Path(args.out), pairing)
    print(
        f"Enrolled {args.node_id}; copy {args.out} to /var/lib/musegadget/pairing.json "
        "on the device (mode 0600)"
    )
    return 0


def ca_fingerprint(ca_pem: bytes) -> str:
    """SHA-256 of the CA in openssl's colon-separated form, for pinning by the app."""
    digest = x509.load_pem_x509_certificate(ca_pem).fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in digest)


def cmd_grant(args: argparse.Namespace, state: Path) -> int:
    config = HostConfig.load(state / CONFIG_FILE)
    code = Tokens(Store.open(state / DB_FILE)).create_grant()
    print("Enter these in the pairing app (the code works once, for 10 minutes):")
    print(f"Code: {code}")
    print(f"Host: {config.api_url}")
    print(f"CA SHA-256: {ca_fingerprint((state / 'ca.pem').read_bytes())}")
    return 0


def current_ssid() -> str | None:
    """The Wi-Fi network this machine is on, if NetworkManager knows it."""
    nmcli = shutil.which("nmcli")
    if nmcli is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed arguments
            [nmcli, "-t", "-f", "active,ssid", "dev", "wifi"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        active, _, ssid = line.partition(":")
        if active == "yes" and ssid:
            return ssid
    return None


async def _pair(args: argparse.Namespace, state: Path) -> int:
    print("Looking for gadgets in setup mode...")
    gadgets = await ble_client.scan(args.scan_timeout)
    if args.scan_only:
        for g in gadgets:
            print(f"{g.name:<20} {g.address}  {g.rssi} dBm")
        return 0 if gadgets else _fail("no gadget in setup mode found")
    if args.name:
        gadgets = [g for g in gadgets if g.name == args.name]
    if not gadgets:
        return _fail("no gadget in setup mode found" + (f" named {args.name}" if args.name else ""))
    if len(gadgets) > 1:
        names = ", ".join(g.name for g in gadgets)
        return _fail(f"several gadgets in setup mode ({names}); choose one with --name")
    gadget = gadgets[0]

    ssid = args.ssid or current_ssid()
    if not ssid:
        return _fail("no Wi-Fi network given; pass --ssid")
    password = getpass.getpass(f"Wi-Fi password for {ssid}: ")

    config = HostConfig.load(state / CONFIG_FILE)
    tokens = Tokens(Store.open(state / DB_FILE))
    started = tokens.now()
    print(f"Connecting to {gadget.name}...")
    try:
        async with ble_client.BleLink(gadget.address) as link:
            client = pair.PairingClient(link)
            info = await client.pair(
                on_confirm_wait=lambda info: print(
                    f"Press the button on {gadget.name} to confirm (60 s)..."
                )
            )
            print(f"Confirmed: {info['node_id']}. Sending Wi-Fi and host details...")
            node_id = await pair.provision(
                client,
                info,
                tokens=tokens,
                config=config,
                ssid=ssid,
                password=password,
                ca_pem=(state / "ca.pem").read_text(),
                noise_static_pub=pki.noise_public_b64(
                    pki.load_noise_key(state / "noise_static.key")
                ),
                display_name=args.display_name or gadget.name,
            )
    except pair.PairingFailed as exc:
        return _fail(f"pairing {gadget.name} failed: {exc}")
    print(f"Provisioned {node_id}. Waiting for it to reach the host...")
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        row = tokens.store.db.execute(
            "SELECT last_seen FROM devices WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row and row["last_seen"] and row["last_seen"] >= started:
            print(f"{node_id} reached the host.")
            return 0
        await asyncio.sleep(1)
    print(f"{node_id} hasn't reached the host yet; check its log or `musehost devices list`.")
    return 0


def cmd_pair(args: argparse.Namespace, state: Path) -> int:
    return asyncio.run(_pair(args, state))


def _when(epoch: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)) if epoch else "-"


def _online_nodes(state: Path) -> set[str] | None:
    """Node ids the running server has online, or None if it isn't running."""
    try:
        reply = admin.request(state, {"op": "devices"}, timeout=3)
    except admin.AdminUnavailable:
        return None
    return {d["node_id"] for d in reply.get("devices", []) if d.get("online")}


def cmd_devices_list(args: argparse.Namespace, state: Path) -> int:
    rows = Store.open(state / DB_FILE).devices()
    if not rows:
        print("No devices enrolled.")
        return 0
    online = _online_nodes(state)
    print(f"{'NODE ID':<16} {'NAME':<20} {'ENROLLED':<16} {'LAST SEEN':<16} {'STATUS':<8} ONLINE")
    for row in rows:
        status = "revoked" if row["revoked_at"] else "active"
        live = "?" if online is None else ("yes" if row["node_id"] in online else "no")
        print(
            f"{row['node_id']:<16} {row['display_name'][:20]:<20} "
            f"{_when(row['enrolled_at']):<16} {_when(row['last_seen']):<16} {status:<8} {live}"
        )
    return 0


def cmd_invoke(args: argparse.Namespace, state: Path) -> int:
    """Run a command on an online gadget through the server; prints its result."""
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        print(f"musehost: params are not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("musehost: params must be a JSON object", file=sys.stderr)
        return 2
    message = {
        "op": "invoke",
        "node_id": args.node_id,
        "command": args.command,
        "params": params,
        "timeout_s": args.timeout,
    }
    try:
        reply = admin.request(state, message, timeout=args.timeout + 5)
    except admin.AdminUnavailable:
        print("musehost: the server is not running (start musehost serve)", file=sys.stderr)
        return 2
    if not reply.get("ok"):
        print(f"musehost: {reply.get('error')}", file=sys.stderr)
        return 2
    result = reply["result"]
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_devices_revoke(args: argparse.Namespace, state: Path) -> int:
    # Tell a connected gadget first, while its session still exists; it then
    # wipes its setup and goes back to pairing instead of retrying forever.
    try:
        notified = admin.request(state, {"op": "unpair", "node_id": args.node_id}).get("notified")
    except admin.AdminUnavailable:
        notified = None
    if not Tokens(Store.open(state / DB_FILE)).revoke(args.node_id):
        return _fail(f"no enrolled device {args.node_id}")
    print(f"Revoked {args.node_id}; its next fetch or refresh will be refused.")
    if notified:
        print(f"{args.node_id} was notified and will go back to pairing mode.")
    elif notified is None:
        print(f"The server isn't running, so {args.node_id} wasn't notified.")
    else:
        print(f"{args.node_id} is offline, so it wasn't notified.")
    return 0


def cmd_download_model(args: argparse.Namespace, state: Path) -> int:
    name = args.name or HostConfig.load(state / CONFIG_FILE).speech_model
    if not name:
        return _fail("speech is off (speech_model is empty in host.toml)")
    path = speech.download_model(name, state / "models")
    print(f"Speech model {name} is in {path}")
    return 0


def cmd_download_voice(args: argparse.Namespace, state: Path) -> int:
    name = args.name or HostConfig.load(state / CONFIG_FILE).tts_voice
    if not name:
        return _fail("speech output is off (tts_voice is empty in host.toml)")
    path = voice_out.download_voice(name, state / "models")
    print(f"Voice {name} is in {path}")
    return 0


def cmd_say(args: argparse.Namespace, state: Path) -> int:
    """Speak ``text`` with the configured voice into an MP3, for checking it by hand."""
    engine = voice_out.engine_for(HostConfig.load(state / CONFIG_FILE), state / "models")
    if engine is None:
        return _fail("speech output is off (tts_voice is empty in host.toml)")
    started = time.monotonic()
    try:
        engine.load()
    except FileNotFoundError as exc:
        return _fail(f"{exc}; run `musehost download-voice` first")
    loaded = time.monotonic()
    encoder, mp3, audio_s, first = None, b"", 0.0, None
    for pcm, rate in engine.synthesize(args.text):
        encoder = encoder or voice_out.Mp3Encoder(rate)
        mp3 += encoder.feed(pcm)
        audio_s += len(pcm) / 2 / rate
        first = first if first is not None else time.monotonic() - loaded
    if encoder is not None:
        mp3 += encoder.close()
    done = time.monotonic() - loaded
    Path(args.out).write_bytes(mp3)
    print(
        f"voice loaded in {loaded - started:.1f} s; first sentence ready in {first or 0:.2f} s; "
        f"{audio_s:.1f} s of audio in {done:.2f} s; wrote {len(mp3)} bytes to {args.out}"
    )
    return 0


def cmd_flash(args: argparse.Namespace, state: Path) -> int:
    """Write the self-host firmware to a gadget on this machine's USB."""
    config = HostConfig.load(state / CONFIG_FILE)
    try:
        if args.file:
            path = Path(args.file)
        else:
            print(f"Looking for {args.board} firmware in {config.firmware_repo} releases...")
            path = flash.fetch_release(
                config.firmware_repo,
                board=args.board,
                version=args.version,
                cache=state / "firmware",
                fetch=flash.http_get,
            )
        fw = flash.load_firmware(path, board=args.board)
        port = flash.find_port(args.port, list_ports=flash.serial_ports)
    except flash.FlashError as exc:
        return _fail(str(exc))
    except OSError as exc:  # network trouble, or an unreadable file
        return _fail(f"couldn't get the firmware: {exc}")

    print(f"Firmware: {fw.board} {fw.version} ({fw.chip})")
    print(f"Gadget:   {port}")
    if args.erase_settings:
        print("Settings: will be ERASED (Wi-Fi and pairing); pair the gadget again afterwards")
    else:
        print("Settings: kept (Wi-Fi and pairing stay)")
    if not args.yes:
        try:
            answer = input("Write it? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            return _fail("cancelled; nothing was written")

    sys.stdout.flush()  # our summary before esptool's output, even when piped
    started = time.monotonic()
    try:
        flash.write(fw, port, erase_settings=args.erase_settings, esptool=flash.run_esptool)
    except flash.FlashError as exc:
        return _fail(str(exc))
    print(f"Flashed {fw.board} {fw.version} in {time.monotonic() - started:.0f} s.")
    if args.erase_settings:
        print("It starts in setup mode: run `musehost pair` next.")
    else:
        print("A paired gadget reconnects by itself; a new one waits for `musehost pair`.")
    return 0


def cmd_transcribe(args: argparse.Namespace, state: Path) -> int:
    """Transcribe one WAV with the configured model; for checking speech by hand."""
    engine = speech.engine_for(HostConfig.load(state / CONFIG_FILE), state / "models")
    if engine is None:
        return _fail("speech is off (speech_model is empty in host.toml)")
    started = time.monotonic()
    try:
        engine.load()
    except FileNotFoundError as exc:
        return _fail(f"{exc}; run `musehost download-model` first")
    loaded = time.monotonic()
    samples, rate = speech.decode_wav(Path(args.file))
    text = " ".join(engine.transcribe(samples, rate).split())
    done = time.monotonic()
    print(text or "(nothing heard)")
    print(
        f"{len(samples) / rate:.1f} s of audio; model loaded in {loaded - started:.1f} s, "
        f"transcribed in {done - loaded:.2f} s"
    )
    return 0


def cmd_chat(args: argparse.Namespace, state: Path) -> int:
    """Talk to Clio from the terminal (Ctrl-D or /quit to leave)."""
    new = args.new
    print("Talking to Clio" + (f" with {args.device}'s tools" if args.device else "") + ".")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt, StopIteration):
            print()
            return 0
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return 0
        message = {"op": "chat", "message": line, "device": args.device, "new": new}
        new = False
        print("clio> ", end="", flush=True)
        try:
            for item in admin.stream(state, message):
                if "text" in item:
                    print(item["text"], end="", flush=True)
        except admin.AdminUnavailable:
            print()
            print("musehost: the server is not running (start musehost serve)", file=sys.stderr)
            return 2
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="musehost")
    parser.add_argument(
        "--state-dir", help="host state directory (default ./state, or $MUSEHOST_STATE_DIR)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create the CA, certs, Noise key and database")
    init.add_argument(
        "--hostname",
        action="append",
        help="DNS or mDNS name for the host (repeatable; first is provisioned)",
    )
    init.add_argument("--ip", action="append", help="IP address for the host (repeatable)")
    init.add_argument("--port", type=int, default=8443)
    init.add_argument("--vm-id", default="home")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    serve = sub.add_parser("serve", help="serve the device API over TLS")
    serve.add_argument("--port", type=int, help="override the port in host.toml")
    serve.add_argument("--bind", default="0.0.0.0", help="address to listen on")  # noqa: S104
    serve.set_defaults(func=cmd_serve)

    enroll = sub.add_parser("enroll", help="enroll a gadget by hand and write its pairing.json")
    enroll.add_argument("node_id", help="the gadget's node id, e.g. homelink-abcdef")
    enroll.add_argument("--display-name", default="")
    enroll.add_argument("--out", required=True, help="where to write pairing.json")
    enroll.set_defaults(func=cmd_enroll)

    sub.add_parser("grant", help="create a one-time enrollment code for the app").set_defaults(
        func=cmd_grant
    )

    pairing = sub.add_parser("pair", help="pair and provision a gadget over Bluetooth")
    pairing.add_argument("--name", help="the gadget's BLE name, e.g. MuseGadgetABCDEF")
    pairing.add_argument("--ssid", help="Wi-Fi network for the gadget (default: this one's)")
    pairing.add_argument("--display-name", default="")
    pairing.add_argument("--scan-only", action="store_true", help="list gadgets and stop")
    pairing.add_argument("--scan-timeout", type=float, default=8.0)
    pairing.add_argument(
        "--wait", type=float, default=90.0, help="seconds to wait for its first contact"
    )
    pairing.set_defaults(func=cmd_pair)

    invoke = sub.add_parser("invoke", help="run a command on an online gadget")
    invoke.add_argument("node_id")
    invoke.add_argument("command", help="one of the gadget's commands, e.g. device.health")
    invoke.add_argument("params", nargs="?", default="", help="JSON object of parameters")
    invoke.add_argument("--timeout", type=float, default=30.0)
    invoke.set_defaults(func=cmd_invoke)

    download = sub.add_parser("download-model", help="fetch the speech model once")
    download.add_argument("name", nargs="?", help="model name (default: host.toml's speech_model)")
    download.set_defaults(func=cmd_download_model)

    download_voice = sub.add_parser("download-voice", help="fetch the Piper voice once")
    download_voice.add_argument("name", nargs="?", help="voice (default: host.toml's tts_voice)")
    download_voice.set_defaults(func=cmd_download_voice)

    say = sub.add_parser("say", help="speak text with Clio's voice into an MP3")
    say.add_argument("text")
    say.add_argument("--out", default="say.mp3")
    say.set_defaults(func=cmd_say)

    flash_cmd = sub.add_parser("flash", help="write the self-host firmware to a gadget on USB")
    flash_cmd.add_argument("--board", default="cores3", help="board name (default cores3)")
    flash_cmd.add_argument("--port", help="serial port (default: the one Espressif gadget)")
    source = flash_cmd.add_mutually_exclusive_group()
    source.add_argument("--version", help="release version, e.g. 0.1.0 (default: newest)")
    source.add_argument("--file", help="a local firmware zip instead of a release")
    flash_cmd.add_argument(
        "--erase-settings", action="store_true", help="also clear the gadget's Wi-Fi and pairing"
    )
    flash_cmd.add_argument("--yes", action="store_true", help="don't ask before writing")
    flash_cmd.set_defaults(func=cmd_flash)

    transcribe = sub.add_parser("transcribe", help="transcribe a WAV with the speech model")
    transcribe.add_argument("file")
    transcribe.set_defaults(func=cmd_transcribe)

    chat_parser = sub.add_parser("chat", help="talk to Clio from this terminal")
    chat_parser.add_argument("--device", help="let Clio use this gadget's tools (node id)")
    chat_parser.add_argument("--new", action="store_true", help="start a fresh conversation")
    chat_parser.set_defaults(func=cmd_chat)

    devices = sub.add_parser("devices", help="list or revoke enrolled gadgets")
    devices_sub = devices.add_subparsers(dest="devices_command", required=True)
    devices_sub.add_parser("list", help="list enrolled gadgets").set_defaults(func=cmd_devices_list)
    revoke = devices_sub.add_parser("revoke", help="revoke a gadget's tokens")
    revoke.add_argument("node_id")
    revoke.set_defaults(func=cmd_devices_revoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args, state_dir(args.state_dir))


if __name__ == "__main__":
    sys.exit(main())
