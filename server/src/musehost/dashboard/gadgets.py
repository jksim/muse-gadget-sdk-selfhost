"""Gadget pages: the list (live over SSE), one gadget's detail, health, revoke."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

from musehost.dashboard.jobs import Busy, Jobs
from musehost.dashboard.web import page, require_csrf, templates
from musehost.hub import DeviceOffline, InvokeTimeout

log = logging.getLogger(__name__)
HEALTH_TIMEOUT_S = 15
PAIR_TIMEOUT_S = 180
PAIR_WAIT_S = 60  # for the gadget to reach the host after provisioning
FLASH_TIMEOUT_S = 600
BOARDS = ("cores3",)


def when(epoch) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)) if epoch else "-"


def gadget_rows(hub, store) -> list[dict]:
    """Enrolled gadgets from the store, with what the hub knows of them now."""
    live = {d.node_id: d for d in hub.devices()}
    rows = []
    for row in store.devices():
        device = live.get(row["node_id"])
        rows.append(
            {
                "node_id": row["node_id"],
                "name": row["display_name"]
                or (device.display_name if device else "")
                or row["node_id"],
                "online": bool(device and device.online),
                "version": device.version if device else "",
                "platform": device.platform if device else "",
                "enrolled": when(row["enrolled_at"]),
                "last_seen": when(row["last_seen"]),
                "revoked": bool(row["revoked_at"]),
            }
        )
    return rows


def _sse(event: str, html: str) -> str:
    data = "".join(f"data: {line}\n" for line in html.splitlines() or [""])
    return f"event: {event}\n{data}\n"


async def list_events(hub, store, is_disconnected, keepalive_s: float = 15):
    """The gadget table now, then again whenever the hub changes, until the browser goes."""
    loop = asyncio.get_running_loop()
    changed = asyncio.Event()

    def on_change():
        loop.call_soon_threadsafe(changed.set)

    hub.on_change(on_change)
    try:
        while True:
            changed.clear()
            html = templates.get_template("_gadget_rows.html").render(rows=gadget_rows(hub, store))
            yield _sse("gadgets", html)
            while not changed.is_set():
                if await is_disconnected():
                    return
                try:
                    await asyncio.wait_for(changed.wait(), keepalive_s)
                except TimeoutError:
                    yield ": keepalive\n\n"
    finally:
        hub.off_change(on_change)


def routes(state: Path, parent) -> list[Route]:
    if not hasattr(parent.state, "jobs"):
        parent.state.jobs = Jobs()

    def jobs() -> Jobs:
        return parent.state.jobs

    def hub():
        return parent.state.hub

    def store():
        return parent.state.tokens.store

    def find(node_id: str) -> dict | None:
        return next((r for r in gadget_rows(hub(), store()) if r["node_id"] == node_id), None)

    async def gadget_list(request: Request):
        return page(
            request, "gadgets.html", {"title": "Gadgets", "rows": gadget_rows(hub(), store())}
        )

    async def events(request: Request):
        return StreamingResponse(
            list_events(hub(), store(), request.is_disconnected),
            media_type="text/event-stream",
            headers={"x-accel-buffering": "no"},
        )

    async def detail(request: Request):
        node_id = request.path_params["node_id"]
        gadget = find(node_id)
        if gadget is None:
            return PlainTextResponse("No such gadget", status_code=404)
        device = next((d for d in hub().devices() if d.node_id == node_id), None)
        allowed = set(parent.state.config.brain_tools)
        commands = sorted((device.commands if device else {}).items())
        return page(
            request,
            "gadget.html",
            {
                "title": gadget["name"],
                "gadget": gadget,
                "allowed": allowed,
                "commands": [
                    (name, (spec or {}).get("description", "")) for name, spec in commands
                ],
                "error": request.query_params.get("error"),
            },
        )

    async def health(request: Request):
        if not await require_csrf(request):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        node_id = request.path_params["node_id"]
        if find(node_id) is None:
            return PlainTextResponse("No such gadget", status_code=404)
        context: dict = {}
        try:
            result = await hub().invoke(node_id, "device.health", {}, HEALTH_TIMEOUT_S)
        except DeviceOffline:
            context["message"] = "The gadget is offline."
        except (InvokeTimeout, TimeoutError):
            context["message"] = f"The gadget didn't answer within {HEALTH_TIMEOUT_S} s."
        else:
            if result.ok:
                payload = (
                    result.payload
                    if isinstance(result.payload, dict)
                    else {"result": result.payload}
                )
                context["items"] = sorted(payload.items())
            else:
                context["message"] = f"The gadget said: {result.error or 'failed'}"
        return page(request, "_health.html", context)

    async def revoke(request: Request):
        if not await require_csrf(request):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        node_id = request.path_params["node_id"]
        gadget = find(node_id)
        if gadget is None:
            return PlainTextResponse("No such gadget", status_code=404)
        form = await request.form()
        if str(form.get("confirm", "")).strip() != gadget["name"]:
            return page(
                request,
                "gadget.html",
                {
                    "title": gadget["name"],
                    "gadget": gadget,
                    "allowed": set(),
                    "commands": [],
                    "error": f"Type the gadget's name, {gadget['name']}, to revoke it.",
                },
                status_code=400,
            )
        # Tell it first, while its session exists: it then goes back to pairing.
        told = await hub().unpair(node_id)
        parent.state.tokens.revoke(node_id)
        log.info("dashboard: revoked %s (%s)", node_id, "told" if told else "offline")
        return RedirectResponse("/dashboard/gadgets", status_code=303)

    async def pair_page(request: Request):
        return page(request, "pair.html", {"title": "Pair", "job": jobs().running("pair")})

    async def pair_scan(request: Request):
        from musehost import ble_client, cli

        if not await require_csrf(request):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        if jobs().running("pair") is not None:
            return page(request, "_scan.html", {"message": "A pairing is under way."})
        try:
            found = await ble_client.scan(8.0)
        except Exception as exc:  # no adapter, BlueZ refused...
            log.warning("dashboard: Bluetooth scan failed: %s", type(exc).__name__)
            return page(request, "_scan.html", {"message": f"Bluetooth scan failed: {exc}"})
        return page(request, "_scan.html", {"gadgets": found, "ssid": cli.current_ssid() or ""})

    async def pair_start(request: Request):
        from musehost import ble_client, pair

        if not await require_csrf(request):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        form = await request.form()
        # "<address> <name>": addresses have no spaces; names may.
        address, _, name = str(form.get("gadget", "")).partition(" ")
        ssid, password = str(form.get("ssid", "")), str(form.get("password", ""))
        display_name = str(form.get("display_name", "")).strip()[:64]
        if not address or not ssid or not password:
            return page(
                request,
                "pair.html",
                {
                    "title": "Pair",
                    "error": "Choose a gadget and give the Wi-Fi name and password.",
                },
                status_code=400,
            )
        gadget = ble_client.Gadget(name=name or address, address=address, rssi=0)

        async def work(progress):
            try:
                node_id, reached = await pair.run_pairing(
                    state,
                    gadget,
                    ssid=ssid,
                    password=password,
                    display_name=display_name,
                    wait_s=PAIR_WAIT_S,
                    progress=progress,
                )
            except pair.PairingFailed as exc:
                raise RuntimeError(f"pairing {gadget.name} failed: {exc}") from None
            return {"node_id": node_id, "reached": reached}

        try:
            job = jobs().start("pair", work, PAIR_TIMEOUT_S)
        except Busy:
            return PlainTextResponse("Busy: a pairing is already under way.", status_code=409)
        return RedirectResponse(f"/dashboard/jobs/{job.id}", status_code=303)

    async def job_page(request: Request):
        job = jobs().get(request.path_params["job_id"])
        if job is None:
            return PlainTextResponse("No such job (it may have ended a while ago)", status_code=404)
        return page(
            request,
            "job.html",
            {
                "title": job.kind.capitalize(),
                "job": job,
                "when": lambda t: time.strftime("%H:%M:%S", time.localtime(t)),
            },
        )

    async def job_events(request: Request):
        job = jobs().get(request.path_params["job_id"])
        if job is None:
            return PlainTextResponse("No such job", status_code=404)
        last = request.headers.get("last-event-id")
        after = int(last) if last and last.isdigit() else None
        return StreamingResponse(
            jobs().events(job, request.is_disconnected, after=after),
            media_type="text/event-stream",
            headers={"x-accel-buffering": "no"},
        )

    async def flash_page(request: Request):
        from musehost import flash

        try:  # lists USB devices from sysfs; never opens a port
            ports = [p.device for p in flash.serial_ports() if p.vid == flash.ESPRESSIF_USB_VID]
        except Exception:
            ports = []
        return page(
            request,
            "flash.html",
            {
                "title": "Flash",
                "boards": BOARDS,
                "ports": ports,
                "job": jobs().running("flash"),
                "repo": parent.state.config.firmware_repo,
            },
        )

    async def flash_start(request: Request):
        from musehost import flash

        if not await require_csrf(request):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        form = await request.form()
        board = str(form.get("board", ""))
        version = str(form.get("version", "")).strip().removeprefix(flash.TAG_PREFIX) or None
        erase = form.get("erase") == "on"
        problem = None
        if board not in BOARDS:
            problem = "Unknown board."
        elif version and not flash.VERSION.fullmatch(version):
            problem = "The version looks like 0.2.0 (or empty for the newest)."
        elif erase and str(form.get("confirm", "")).strip().lower() != "erase":
            problem = "To erase the gadget's settings, type erase."
        if problem:
            return PlainTextResponse(problem, status_code=400)
        config = parent.state.config

        async def work(progress):
            loop = asyncio.get_running_loop()

            def say(text):
                loop.call_soon_threadsafe(progress, text)

            running = []  # esptool's process, to kill if the job is stopped

            def blocking():
                fw, port = flash.prepare(config, state, board=board, version=version, progress=say)
                say(f"Firmware: {fw.board} {fw.version} ({fw.chip}); its checksums match.")
                say(f"Gadget: {port}")
                say("Settings: will be erased" if erase else "Settings: kept")
                started = time.monotonic()
                flash.write(
                    fw,
                    port,
                    erase_settings=erase,
                    esptool=lambda args: flash.run_esptool_lines(args, say, running),
                )
                say(f"Flashed {fw.board} {fw.version} in {time.monotonic() - started:.0f} s.")
                return {"version": fw.version, "erased": erase}

            try:
                return await asyncio.to_thread(blocking)
            except asyncio.CancelledError:
                for proc in running:
                    proc.kill()
                raise
            except flash.FlashError as exc:
                raise RuntimeError(str(exc)) from None
            except OSError as exc:
                raise RuntimeError(f"couldn't get the firmware: {exc}") from None

        try:
            job = jobs().start("flash", work, FLASH_TIMEOUT_S)
        except Busy:
            return PlainTextResponse("Busy: a flash is already under way.", status_code=409)
        return RedirectResponse(f"/dashboard/jobs/{job.id}", status_code=303)

    return [
        Route("/flash", flash_page),
        Route("/flash/start", flash_start, methods=["POST"]),
        Route("/pair", pair_page),
        Route("/pair/scan", pair_scan, methods=["POST"]),
        Route("/pair/start", pair_start, methods=["POST"]),
        Route("/jobs/{job_id}", job_page),
        Route("/jobs/{job_id}/events", job_events),
        Route("/gadgets", gadget_list),
        Route("/gadgets/events", events),
        Route("/gadgets/{node_id}", detail),
        Route("/gadgets/{node_id}/health", health, methods=["POST"]),
        Route("/gadgets/{node_id}/revoke", revoke, methods=["POST"]),
    ]
