"""The dashboard's gadget pages: list, detail, health, revoke, live updates."""

import asyncio
import re
import tempfile
from types import SimpleNamespace

from starlette.testclient import TestClient

from musehost.dashboard import auth
from musehost.hub import DeviceOffline, InvokeResult

GOOD = "correct horse battery"
NODE = "homelink-abcdef"
COMMANDS = {
    "device.health": {"description": "Battery, memory, uptime"},
    "display.show_animation": {"description": "Play an animation"},
    "device.ota": {"description": "Install firmware"},
}


class FakeLink:
    def __init__(self, results=None):
        self.sent = []
        self.results = results or {}

    async def send(self, message):
        self.sent.append(message)

    async def invoke(self, command, params, timeout_s):
        result = self.results.get(command, InvokeResult(ok=True, payload={"overall": "ok"}))
        if isinstance(result, Exception):
            raise result
        return result


def dashboard(state):
    from musehost.app import create_app

    app = create_app(state)
    auth.set_password(state, GOOD)
    client = TestClient(app, base_url="https://muse-host.local")
    client.post("/dashboard/login", data={"password": GOOD})
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/dashboard").text)
    return app, client, csrf.group(1)


def enroll(app, node=NODE, name="Kitchen"):
    app.state.tokens.enroll(node, name)


def online(app, link=None, node=NODE, name="Kitchen"):
    link = link or FakeLink()
    app.state.hub.register(
        node, {"display_name": name, "commands_v2": COMMANDS, "version": "0.1.1"}, link
    )
    return link


def test_the_list_shows_enrolled_gadgets_with_their_online_state(state):
    app, client, _ = dashboard(state)
    enroll(app)
    enroll(app, "homelink-123456", "Desk")
    online(app)
    text = client.get("/dashboard/gadgets").text
    assert "Kitchen" in text and "Desk" in text
    kitchen = text[text.index("Kitchen") :]
    assert "online" in kitchen.split("</tr>")[0] and "0.1.1" in kitchen.split("</tr>")[0]
    desk = text[text.index("Desk") :]
    assert "offline" in desk.split("</tr>")[0]
    assert "Gadgets" in client.get("/dashboard").text  # back in the nav


def test_the_list_is_escaped(state):
    app, client, _ = dashboard(state)
    enroll(app, name="<script>alert(1)</script>")
    text = client.get("/dashboard/gadgets").text
    assert "<script>alert(1)" not in text and "&lt;script&gt;" in text


def test_the_detail_page_marks_the_commands_clio_may_use(state):
    app, client, _ = dashboard(state)
    enroll(app)
    online(app)
    text = client.get(f"/dashboard/gadgets/{NODE}").text
    assert "device.health" in text and "display.show_animation" in text
    health_row = text[text.index("device.health") :].split("</tr>")[0]
    assert "Clio" in health_row
    ota_row = text[text.index("device.ota") :].split("</tr>")[0]
    assert "Clio" not in ota_row


def test_an_unknown_gadget_is_a_404(state):
    _, client, _ = dashboard(state)
    assert client.get("/dashboard/gadgets/homelink-nope").status_code == 404


def test_health_shows_the_gadgets_answer(state):
    app, client, csrf = dashboard(state)
    enroll(app)
    online(
        app,
        FakeLink(
            {
                "device.health": InvokeResult(
                    ok=True,
                    payload={"battery_pct": 82, "uptime_s": 3600, "wifi": {"rssi": -51}},
                )
            }
        ),
    )
    text = client.post(f"/dashboard/gadgets/{NODE}/health", headers={"X-CSRF-Token": csrf}).text
    assert "battery_pct" in text and "82" in text and "-51" in text


def test_health_of_an_offline_gadget_says_so(state):
    app, client, csrf = dashboard(state)
    enroll(app)
    text = client.post(f"/dashboard/gadgets/{NODE}/health", headers={"X-CSRF-Token": csrf}).text
    assert "offline" in text.lower()
    online(app, FakeLink({"device.health": DeviceOffline(NODE)}))
    text = client.post(f"/dashboard/gadgets/{NODE}/health", headers={"X-CSRF-Token": csrf}).text
    assert "offline" in text.lower()


def test_health_needs_the_csrf_token(state):
    app, client, _ = dashboard(state)
    enroll(app)
    online(app)
    assert client.post(f"/dashboard/gadgets/{NODE}/health").status_code == 403


def test_revoke_needs_the_typed_name(state):
    app, client, csrf = dashboard(state)
    enroll(app)
    link = online(app)
    refused = client.post(
        f"/dashboard/gadgets/{NODE}/revoke",
        data={"csrf": csrf, "confirm": "kitchen?"},
        follow_redirects=False,
    )
    assert refused.status_code == 400
    assert link.sent == []
    assert app.state.tokens.store.devices()[0]["revoked_at"] is None


def test_revoke_tells_the_gadget_and_cuts_it_off(state):
    app, client, csrf = dashboard(state)
    enroll(app)
    link = online(app)
    done = client.post(
        f"/dashboard/gadgets/{NODE}/revoke",
        data={"csrf": csrf, "confirm": "Kitchen"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert {"type": "event", "event": "link.unpaired"} in link.sent
    assert app.state.tokens.store.devices()[0]["revoked_at"] is not None
    assert "revoked" in client.get("/dashboard/gadgets").text


def test_revoke_needs_the_csrf_token(state):
    app, client, _ = dashboard(state)
    enroll(app)
    response = client.post(f"/dashboard/gadgets/{NODE}/revoke", data={"confirm": "Kitchen"})
    assert response.status_code == 403
    assert app.state.tokens.store.devices()[0]["revoked_at"] is None


def test_hub_changes_push_the_list_over_sse_and_closing_stops_listening(state):
    from musehost.dashboard import gadgets

    app, _, _ = dashboard(state)
    enroll(app)
    hub = app.state.hub
    gone = asyncio.Event()

    async def disconnected():
        return gone.is_set()

    async def scenario():
        stream = gadgets.list_events(hub, app.state.tokens.store, disconnected, keepalive_s=0.05)
        first = await anext(stream)  # the list as it is now
        assert first.startswith("event: gadgets\n") and "offline" in first
        link = online(app)
        update = await anext(stream)
        while "online" not in update:
            update = await anext(stream)
        assert all(line.startswith(("event:", "data:")) for line in update.strip().splitlines())
        hub.unregister(NODE, link)
        update = await anext(stream)
        while "offline" not in update:
            update = await anext(stream)
        listeners = len(hub._change_listeners)
        gone.set()
        rest = [chunk async for chunk in stream]
        assert len(rest) <= 1
        assert len(hub._change_listeners) == listeners - 1

    asyncio.run(asyncio.wait_for(scenario(), 5))


# -- T7: jobs and pairing from the browser ------------------------------------------------

import contextlib  # noqa: E402
import logging  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

WIFI = "hunter22-PLANTED"


@contextlib.asynccontextmanager
async def async_dashboard(state):
    """The app on one event loop (no lifespan), so background jobs keep running."""
    from musehost.app import create_app

    app = create_app(state)
    auth.set_password(state, GOOD)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://muse-host.local") as c:
        await c.post("/dashboard/login", data={"password": GOOD})
        page = (await c.get("/dashboard")).text
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        yield app, c, csrf


@pytest.fixture
def ble(monkeypatch, state):
    from test_cli import FakeBle

    from musehost import ble_client
    from musehost.store import Store
    from musehost.tokens import Tokens

    gadgets = [ble_client.Gadget("MuseGadgetABCDEF", "AA:BB:CC:DD:EE:FF", -50)]

    async def scan(timeout=8.0):
        return list(gadgets)

    monkeypatch.setattr(ble_client, "scan", scan)
    monkeypatch.setattr(ble_client, "BleLink", FakeBle)
    FakeBle.reach_host = True
    FakeBle.tokens = Tokens(Store.open(state / "musehost.db"))
    return FakeBle


def start_form(csrf, **extra):
    return {
        "csrf": csrf,
        "gadget": "AA:BB:CC:DD:EE:FF MuseGadgetABCDEF",
        "ssid": "HomeNet",
        "password": WIFI,
        "display_name": "Kitchen",
        **extra,
    }


async def finished(app, job_id):
    job = app.state.jobs.get(job_id)
    await asyncio.wait_for(job.done.wait(), 10)
    return job


def everything_said(job, *pages):
    return "\n".join([*(text for _, text in job.events), *pages])


def test_scanning_offers_the_gadgets_in_setup_mode(state, ble, monkeypatch):
    from musehost import cli

    monkeypatch.setattr(cli, "current_ssid", lambda: "PiNet")

    async def scenario():
        async with async_dashboard(state) as (app, c, csrf):
            text = (await c.post("/dashboard/pair/scan", headers={"X-CSRF-Token": csrf})).text
            assert 'value="AA:BB:CC:DD:EE:FF MuseGadgetABCDEF"' in text and 'value="PiNet"' in text
            assert (await c.post("/dashboard/pair/scan")).status_code == 403

    asyncio.run(scenario())


def test_a_pair_job_streams_its_steps_and_enrolls_the_gadget(state, ble, caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        async with async_dashboard(state) as (app, c, csrf):
            started = await c.post("/dashboard/pair/start", data=start_form(csrf))
            assert started.status_code == 303
            job_id = started.headers["location"].rsplit("/", 1)[1]
            job = await finished(app, job_id)
            assert job.state == "succeeded", job.events
            said = [text for _, text in job.events]
            assert any("Press the button on MuseGadgetABCDEF" in s for s in said)
            assert any("reached the host" in s for s in said)
            assert app.state.tokens.store.devices()[0]["node_id"] == "homelink-abcdef"
            page = (await c.get(f"/dashboard/jobs/{job_id}")).text
            assert "/dashboard/gadgets/homelink-abcdef" in page
            assert WIFI not in everything_said(job, page, caplog.text)

    asyncio.run(scenario())


def test_a_second_pair_job_while_one_runs_is_refused(state, ble, monkeypatch):
    gate = None
    original = ble.__aenter__

    async def slow(self):
        await gate.wait()
        return await original(self)

    monkeypatch.setattr(ble, "__aenter__", slow)

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        async with async_dashboard(state) as (app, c, csrf):
            first = await c.post("/dashboard/pair/start", data=start_form(csrf))
            second = await c.post("/dashboard/pair/start", data=start_form(csrf))
            assert second.status_code == 409 and "busy" in second.text.lower()
            gate.set()
            await finished(app, first.headers["location"].rsplit("/", 1)[1])

    asyncio.run(scenario())


def test_a_provisioning_failure_fails_the_job_and_revokes_the_tokens(state, ble, monkeypatch):
    original = ble.__aenter__

    async def failing(self):
        link = await original(self)
        link.provision_outcome = "auth_failed"
        return link

    monkeypatch.setattr(ble, "__aenter__", failing)

    async def scenario():
        async with async_dashboard(state) as (app, c, csrf):
            started = await c.post("/dashboard/pair/start", data=start_form(csrf))
            job = await finished(app, started.headers["location"].rsplit("/", 1)[1])
            assert job.state == "failed" and "auth_failed" in job.reason
            assert app.state.tokens.store.devices()[0]["revoked_at"] is not None
            assert WIFI not in everything_said(job, job.reason)

    asyncio.run(scenario())


def test_a_pair_job_that_takes_too_long_fails_and_revokes(state, ble, monkeypatch):
    from musehost import pair
    from musehost.dashboard import gadgets

    monkeypatch.setattr(gadgets, "PAIR_TIMEOUT_S", 1)

    send, receive = pair.PairingClient.send_encrypted, pair.PairingClient.receive_encrypted

    async def sending(self, message):
        self.provisioned = message.get("action") == "provision_v2"
        return await send(self, message)

    async def silent_after_provisioning(self, timeout=None):
        if getattr(self, "provisioned", False):
            await asyncio.sleep(3600)  # the gadget never reports back
        return await receive(self, timeout=timeout)

    monkeypatch.setattr(pair.PairingClient, "send_encrypted", sending)
    monkeypatch.setattr(pair.PairingClient, "receive_encrypted", silent_after_provisioning)

    async def scenario():
        async with async_dashboard(state) as (app, c, csrf):
            started = await c.post("/dashboard/pair/start", data=start_form(csrf))
            job = await finished(app, started.headers["location"].rsplit("/", 1)[1])
            assert job.state == "failed" and "too long" in job.reason
            rows = app.state.tokens.store.devices()
            assert rows and all(r["revoked_at"] is not None for r in rows)

    asyncio.run(scenario())


def test_starting_a_pair_job_needs_the_csrf_token(state, ble):
    async def scenario():
        async with async_dashboard(state) as (app, c, csrf):
            form = start_form(csrf)
            del form["csrf"]
            assert (await c.post("/dashboard/pair/start", data=form)).status_code == 403

    asyncio.run(scenario())


def test_job_events_replay_then_follow_and_resume_after_a_reconnect():
    from musehost.dashboard.jobs import Jobs

    async def scenario():
        jobs = Jobs()
        release = asyncio.Event()

        async def work(progress):
            progress("alpha")
            await release.wait()
            progress("bravo")
            return {"answer": 42}

        job = jobs.start("pair", work, timeout_s=5)

        async def never():
            return False

        stream = jobs.events(job, never)
        first = await anext(stream)
        assert "id: 0\n" in first and "alpha" in first
        release.set()
        rest = [chunk async for chunk in stream]
        assert any("bravo" in chunk for chunk in rest)
        assert rest[-1].startswith("event: done\n")
        assert job.result == {"answer": 42}

        again = [chunk async for chunk in jobs.events(job, never, after=0)]
        assert not any("alpha" in chunk for chunk in again)
        assert any("bravo" in chunk for chunk in again)

    asyncio.run(scenario())


def test_job_lines_are_escaped():
    from musehost.dashboard.jobs import Jobs

    async def scenario():
        jobs = Jobs()

        async def work(progress):
            progress("<img src=x onerror=alert(1)>")

        job = jobs.start("pair", work, timeout_s=5)
        await job.done.wait()

        async def never():
            return False

        chunks = "".join([chunk async for chunk in jobs.events(job, never)])
        assert "<img" not in chunks and "&lt;img" in chunks

    asyncio.run(scenario())


# -- T8: flashing from the browser, and the service's device sandbox -------------------------


@pytest.fixture
def fw(monkeypatch, tmp_path):
    """A fake release on 'GitHub', one gadget on USB, and esptool stubbed out."""
    import json

    from test_flash import make_zip, port, releases

    from musehost import flash

    calls = []
    zips = {"good": make_zip(tmp_path / "good.zip").read_bytes()}

    def fetch(url):
        if "api.github" in url:
            return json.dumps(releases("selfhost-v0.2.0")).encode()
        return zips["good"]

    def esptool(args, on_line, holder=None):
        calls.append(list(args))
        on_line("Connecting....")
        on_line(f"Writing {args[-1].rsplit('/', 1)[-1]} at 0x00010000... (50 %)")
        on_line("Hash of data verified.")
        return 0

    monkeypatch.setattr(flash, "http_get", fetch)
    monkeypatch.setattr(flash, "serial_ports", lambda: [port("/dev/ttyACM0")])
    monkeypatch.setattr(flash, "run_esptool_lines", esptool)
    return SimpleNamespace(calls=calls, zips=zips, tmp=tmp_path, make_zip=make_zip)


def flash_form(csrf, **extra):
    return {"csrf": csrf, "board": "cores3", "version": "", **extra}


def test_the_flash_page_shows_the_gadget_on_usb(state, fw):
    _, client, _ = dashboard(state)
    text = client.get("/dashboard/flash").text
    assert "/dev/ttyACM0" in text and "cores3" in text


def test_a_flash_job_streams_its_steps_and_writes_the_release(state, fw):
    async def scenario():
        async with async_dashboard(state) as (app, c, csrf):
            started = await c.post("/dashboard/flash/start", data=flash_form(csrf))
            assert started.status_code == 303
            job = await finished(app, started.headers["location"].rsplit("/", 1)[1])
            assert job.state == "succeeded", job.events
            said = "\n".join(text for _, text in job.events)
            for step in (
                "Looking for cores3",
                "0.2.0",
                "/dev/ttyACM0",
                "Hash of data verified",
                "Flashed cores3 0.2.0",
            ):
                assert step in said, step
            assert tempfile.gettempdir() + "/" not in said  # no image paths
            assert len(fw.calls) == 1 and "write-flash" in fw.calls[0]
            assert "erase-region" not in fw.calls[0]

    asyncio.run(scenario())


def test_a_tampered_release_fails_before_writing(state, fw):
    fw.zips["good"] = fw.make_zip(fw.tmp / "bad.zip", tamper="bootloader.bin").read_bytes()

    async def scenario():
        async with async_dashboard(state) as (app, c, csrf):
            started = await c.post("/dashboard/flash/start", data=flash_form(csrf))
            job = await finished(app, started.headers["location"].rsplit("/", 1)[1])
            assert job.state == "failed" and "checksum" in job.reason
            assert fw.calls == []

    asyncio.run(scenario())


def test_erasing_settings_needs_the_typed_word(state, fw):
    async def scenario():
        async with async_dashboard(state) as (app, c, csrf):
            refused = await c.post(
                "/dashboard/flash/start", data=flash_form(csrf, erase="on", confirm="yes")
            )
            assert refused.status_code == 400 and fw.calls == []
            started = await c.post(
                "/dashboard/flash/start", data=flash_form(csrf, erase="on", confirm="erase")
            )
            job = await finished(app, started.headers["location"].rsplit("/", 1)[1])
            assert job.state == "succeeded"
            assert [c[c.index("--after") + 2] for c in fw.calls] == ["erase-region", "write-flash"]

    asyncio.run(scenario())


def test_a_bad_version_or_board_is_refused(state, fw):
    async def scenario():
        async with async_dashboard(state) as (app, c, csrf):
            for form in (
                flash_form(csrf, version="0.2; rm -rf /"),
                flash_form(csrf, board="../etc"),
            ):
                assert (await c.post("/dashboard/flash/start", data=form)).status_code == 400
            assert fw.calls == []

    asyncio.run(scenario())


def test_starting_a_flash_needs_the_csrf_token(state, fw):
    async def scenario():
        async with async_dashboard(state) as (app, c, csrf):
            form = flash_form(csrf)
            del form["csrf"]
            assert (await c.post("/dashboard/flash/start", data=form)).status_code == 403

    asyncio.run(scenario())


def test_esptool_lines_come_through_a_pipe_without_image_paths(tmp_path, monkeypatch):
    import sys

    from musehost import flash

    image = tmp_path / "app.bin"
    image.write_bytes(b"x")
    script = (
        "import sys\n"
        f"print('Writing {image} at 0x10000', flush=True)\n"
        "for p in (10, 50, 100):\n"
        "    sys.stdout.write(f'\\rWriting at 0x10000 [{p:3d} %]'); sys.stdout.flush()\n"
        "print()\nprint('Hard resetting via RTS pin...')\n"
    )
    monkeypatch.setattr(flash, "_esptool_command", lambda args: [sys.executable, "-c", script])
    lines = []
    assert flash.run_esptool_lines([str(image)], lines.append) == 0
    assert any("app.bin" in line for line in lines)
    assert not any(str(tmp_path) in line for line in lines)
    assert any("100 %" in line for line in lines) and lines[-1].startswith("Hard resetting")


def test_the_service_can_reach_usb_serial_and_nothing_else():
    from pathlib import Path

    unit = (Path(__file__).parents[1] / "deploy" / "musehost.service").read_text()
    settings = [
        line.strip()
        for line in unit.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    ]
    assert "PrivateDevices=true" not in settings
    assert "DevicePolicy=closed" in settings
    assert [s for s in settings if s.startswith("DeviceAllow=")] == ["DeviceAllow=char-ttyACM rw"]
    groups = next(s for s in settings if s.startswith("SupplementaryGroups=")).split("=", 1)[1]
    assert set(groups.split()) == {"dialout", "plugdev", "bluetooth"}
