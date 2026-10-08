"""The dashboard's foundation: password, sessions, auth, status and settings."""

import io
import json
import stat

import pytest

from musehost.cli import main
from musehost.dashboard import auth
from musehost.store import Store

GOOD = "correct horse battery"


# -- T1: the password and `musehost dashboard-password` -----------------------------------


def test_with_no_password_the_dashboard_is_off(state):
    assert not auth.is_enabled(state)
    assert not auth.check_password(state, GOOD)


def test_a_set_password_is_stored_hashed_and_private(state):
    auth.set_password(state, GOOD)
    path = state / auth.PASSWORD_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert GOOD not in text
    record = json.loads(text)
    assert record["scheme"] == "scrypt" and len(bytes.fromhex(record["salt"])) == 16
    assert auth.is_enabled(state)
    assert auth.check_password(state, GOOD)
    assert not auth.check_password(state, GOOD + "!")
    assert not auth.check_password(state, "")


def test_a_short_password_is_refused_and_nothing_changes(state):
    auth.set_password(state, GOOD)
    before = (state / auth.PASSWORD_FILE).read_text()
    with pytest.raises(ValueError, match="12"):
        auth.set_password(state, "too short")
    assert (state / auth.PASSWORD_FILE).read_text() == before


def test_the_same_password_gets_a_new_salt_each_time(state):
    auth.set_password(state, GOOD)
    first = json.loads((state / auth.PASSWORD_FILE).read_text())["salt"]
    auth.set_password(state, GOOD)
    assert json.loads((state / auth.PASSWORD_FILE).read_text())["salt"] != first


def add_session(state):
    store = Store.open(state / "musehost.db")
    store.db.execute(
        "INSERT INTO dashboard_sessions (token_hash, csrf, created_at, last_seen)"
        " VALUES ('h', 'c', 0, 0)"
    )
    return store


def sessions(store):
    return store.db.execute("SELECT COUNT(*) FROM dashboard_sessions").fetchone()[0]


def test_cli_sets_the_password_from_stdin_and_ends_sessions(state, monkeypatch, capsys):
    store = add_session(state)
    monkeypatch.setattr("sys.stdin", io.StringIO(GOOD + "\n"))
    assert main(["--state-dir", str(state), "dashboard-password", "--stdin"]) == 0
    out = capsys.readouterr().out
    assert auth.check_password(state, GOOD)
    assert sessions(store) == 0
    assert "/dashboard" in out and "CA SHA-256" in out
    assert GOOD not in out


def test_cli_refuses_a_short_password_from_stdin(state, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["--state-dir", str(state), "dashboard-password", "--stdin"]) != 0
    assert "12" in capsys.readouterr().err
    assert not auth.is_enabled(state)


def test_cli_prompts_twice_and_refuses_a_mismatch(state, monkeypatch, capsys):
    answers = iter([GOOD, GOOD + "x"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["--state-dir", str(state), "dashboard-password"]) != 0
    assert "match" in capsys.readouterr().err
    assert not auth.is_enabled(state)

    answers = iter([GOOD, GOOD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert main(["--state-dir", str(state), "dashboard-password"]) == 0
    assert auth.check_password(state, GOOD)


def test_cli_off_turns_the_dashboard_off_and_ends_sessions(state, capsys):
    auth.set_password(state, GOOD)
    store = add_session(state)
    assert main(["--state-dir", str(state), "dashboard-password", "--off"]) == 0
    assert not auth.is_enabled(state) and sessions(store) == 0
    assert "off" in capsys.readouterr().out.lower()


def test_the_password_never_reaches_the_logs(state, monkeypatch, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr("sys.stdin", io.StringIO(GOOD + "\n"))
    main(["--state-dir", str(state), "dashboard-password", "--stdin"])
    auth.check_password(state, GOOD)
    assert GOOD not in caplog.text


# -- T2: web auth, layout and the CA page ---------------------------------------------------

import hashlib  # noqa: E402
import re  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402


def client_for(state, **kwargs):
    from musehost.app import create_app

    return TestClient(create_app(state), base_url="https://muse-host.local", **kwargs)


def signed_in(state, password=GOOD):
    auth.set_password(state, password)
    client = client_for(state)
    response = client.post("/dashboard/login", data={"password": password}, follow_redirects=False)
    assert response.status_code == 303
    return client


def csrf_of(client):
    page = client.get("/dashboard").text
    return re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)


def test_with_the_dashboard_off_only_the_ca_page_answers(state):
    client = client_for(state)
    assert client.get("/dashboard", follow_redirects=False).status_code == 404
    assert client.get("/dashboard/login", follow_redirects=False).status_code == 404
    assert client.get("/dashboard/static/htmx.min.js").status_code == 404
    ca = client.get("/dashboard/ca")
    assert ca.status_code == 200 and "SHA-256" in ca.text


def test_the_ca_page_offers_this_hosts_ca_and_its_fingerprint(state):
    from musehost.cli import ca_fingerprint

    client = client_for(state)
    pem = (state / "ca.pem").read_bytes()
    assert ca_fingerprint(pem) in client.get("/dashboard/ca").text
    download = client.get("/dashboard/ca.pem")
    assert download.content == pem
    assert "attachment" in download.headers["content-disposition"]


def test_signed_out_requests_go_to_the_login_page(state):
    auth.set_password(state, GOOD)
    client = client_for(state)
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard/login"
    assert client.get("/dashboard/login").status_code == 200


def test_signing_in_sets_a_strict_session_cookie(state):
    auth.set_password(state, GOOD)
    client = client_for(state)
    response = client.post("/dashboard/login", data={"password": GOOD}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"
    cookie = response.headers["set-cookie"]
    for flag in ("Secure", "HttpOnly", "SameSite=strict", "Path=/dashboard"):
        assert flag.lower() in cookie.lower()
    page = client.get("/dashboard")
    assert page.status_code == 200
    for item in ("Status", "Settings", "Log out"):
        assert item in page.text
    # The cookie value is never stored: only its hash.
    token = re.search(r"musehost_dash=([^;]+)", cookie).group(1)
    store = Store.open(state / "musehost.db")
    hashes = [r[0] for r in store.db.execute("SELECT token_hash FROM dashboard_sessions")]
    assert hashes == [hashlib.sha256(token.encode()).hexdigest()]


def test_a_wrong_password_is_refused_then_rate_limited(state):
    auth.set_password(state, GOOD)
    client = client_for(state)
    for _ in range(5):
        wrong = client.post(
            "/dashboard/login", data={"password": "nope nope nope"}, follow_redirects=False
        )
        assert wrong.status_code == 401
    right_but_blocked = client.post(
        "/dashboard/login", data={"password": GOOD}, follow_redirects=False
    )
    assert right_but_blocked.status_code == 429
    assert "set-cookie" not in right_but_blocked.headers


def test_changes_need_the_csrf_token_and_logout_ends_the_session(state):
    client = signed_in(state)
    assert client.post("/dashboard/logout", follow_redirects=False).status_code == 403
    token = csrf_of(client)
    response = client.post(
        "/dashboard/logout", headers={"X-CSRF-Token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert client.get("/dashboard", follow_redirects=False).status_code == 303
    store = Store.open(state / "musehost.db")
    assert store.db.execute("SELECT COUNT(*) FROM dashboard_sessions").fetchone()[0] == 0


def test_a_form_field_csrf_token_works_too(state):
    client = signed_in(state)
    token = csrf_of(client)
    response = client.post("/dashboard/logout", data={"csrf": token}, follow_redirects=False)
    assert response.status_code == 303


def test_an_idle_session_expires(state):
    client = signed_in(state)
    store = Store.open(state / "musehost.db")
    store.db.execute("UPDATE dashboard_sessions SET last_seen = 0")
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_turning_the_dashboard_off_closes_it_even_for_a_signed_in_browser(state):
    client = signed_in(state)
    auth.clear_password(state, Store.open(state / "musehost.db"))
    assert client.get("/dashboard", follow_redirects=False).status_code == 404


def test_every_page_carries_strict_security_headers_and_no_inline_script(state):
    client = signed_in(state)
    for path in ("/dashboard", "/dashboard/ca", "/dashboard/login"):
        response = client.get(path)
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        for script in re.findall(r"<script\b[^>]*>(.*?)</script>", response.text, re.S):
            assert script.strip() == "", f"inline script on {path}"
        assert not re.search(r'(src|href)="https?://', response.text), f"external asset on {path}"


def test_the_vendored_assets_match_their_pinned_checksums(state):
    from pathlib import Path

    import musehost.dashboard

    static = Path(musehost.dashboard.__file__).parent / "static"
    pins = dict(
        re.findall(
            r"^(\S+\.js)\s+.*sha256 ([0-9a-f]{64})$", (static / "THIRD_PARTY.txt").read_text(), re.M
        )
    )
    assert set(pins) == {"htmx.min.js", "sse.min.js"}
    for name, digest in pins.items():
        assert hashlib.sha256((static / name).read_bytes()).hexdigest() == digest
    client = signed_in(state)
    assert client.get("/dashboard/static/htmx.min.js").status_code == 200


def test_the_dashboard_is_served_over_the_hosts_tls(state):
    import ssl
    import urllib.request

    from conftest import serving

    with serving(state) as server:
        port = server.config.port
        context = ssl.create_default_context(cafile=str(state / "ca.pem"))
        with urllib.request.urlopen(
            f"https://localhost:{port}/dashboard/ca", context=context, timeout=5
        ) as response:
            assert response.status == 200 and b"SHA-256" in response.read()


# -- T3: status page and the log ring --------------------------------------------------------


class StateStub:
    def __init__(self, state):
        self.state = state


def status_page(state, monkeypatch, *, brain=None, mcp=None, speech="ready", voice="loading"):
    from musehost.app import create_app

    app = create_app(state)
    app.state.transcriber = StateStub(speech)
    app.state.synthesizer = StateStub(voice)
    if brain is not None:
        app.state.hub.chat_handler = brain
    app.state.mcp_server = mcp
    auth.set_password(state, GOOD)
    client = TestClient(app, base_url="https://muse-host.local")
    client.post("/dashboard/login", data={"password": GOOD})
    return app, client


def test_status_shows_the_services_states(state, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-PLANTED-SECRET")
    brain = SimpleNamespace(provider=SimpleNamespace(name="claude", model="claude-opus-5-5"))
    mcp = SimpleNamespace(config=SimpleNamespace(port=8765))
    app, client = status_page(state, monkeypatch, brain=brain, mcp=mcp)
    app.state.hub.register("homelink-abcdef", {"display_name": "Kitchen"}, object())
    text = client.get("/dashboard").text
    for expected in (
        "claude",
        "claude-opus-5-5",
        "key set",
        "ready",
        "loading",
        "8765",
        "1 online",
        "SHA-256",
        "https://localhost:",
    ):
        assert expected in text, expected
    assert "PLANTED-SECRET" not in text


def test_status_says_when_things_are_off(state, monkeypatch):
    _, client = status_page(state, monkeypatch, speech="off", voice="failed")
    text = client.get("/dashboard").text
    assert "placeholder" in text.lower()  # no brain
    assert "MCP" in text and "off" in text.lower()
    assert "failed" in text


def test_the_log_ring_keeps_the_last_records_and_feeds_the_page(state, monkeypatch):
    import logging

    from musehost.dashboard import logring

    ring = logring.install()
    ring.clear()
    log = logging.getLogger("musehost.test")
    log.setLevel(logging.INFO)
    for i in range(250):
        log.info("line %d", i)
    lines = ring.lines()
    assert len(lines) == logring.KEEP == 200
    assert lines[0].endswith("line 50") and lines[-1].endswith("line 249")
    assert logring.install() is ring  # idempotent: one handler

    _, client = status_page(state, monkeypatch)
    page = client.get("/dashboard/logs")
    assert page.status_code == 200 and "line 249" in page.text
    # The status page polls the log panel; no stream to leak.
    assert 'hx-get="/dashboard/logs"' in client.get("/dashboard").text


def test_secrets_never_reach_the_log_ring(state, monkeypatch):
    from musehost.dashboard import logring

    ring = logring.install()
    ring.clear()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-PLANTED-SECRET")
    _, client = status_page(state, monkeypatch)
    client.get("/dashboard")
    client.post("/dashboard/login", data={"password": "wrong but PLANTED-PASSWORD"})
    joined = "\n".join(ring.lines())
    assert "PLANTED" not in joined
    assert GOOD not in joined


def test_the_transcriber_reports_its_state():
    from musehost.speech import Transcriber

    assert Transcriber(None).state == "off"
    loading = Transcriber(object())
    assert loading.state == "loading"


# -- T4: settings -------------------------------------------------------------------------------


def settings_client(state, monkeypatch):
    from musehost.app import create_app

    app = create_app(state)
    auth.set_password(state, GOOD)
    client = TestClient(app, base_url="https://muse-host.local")
    client.post("/dashboard/login", data={"password": GOOD})
    return app, client, csrf_of(client)


def post_settings(client, csrf, **fields):
    return client.post(
        "/dashboard/settings",
        data={"csrf": csrf, "section": "brain", **fields},
        follow_redirects=False,
    )


BRAIN_FIELDS = {
    "brain_provider": "claude",
    "brain_model": "",
    "brain_base_url": "",
    "brain_effort": "low",
    "brain_web_search": "on",
    "brain_timeout_s": "120",
}


def test_switching_the_brain_takes_effect_without_a_restart(state, monkeypatch):
    from musehost.brain.providers.hermes import HermesProvider
    from musehost.config import HostConfig

    app, client, csrf = settings_client(state, monkeypatch)
    monkeypatch.setenv("HERMES_API_KEY", "hermes-test-key")
    response = post_settings(client, csrf, **{**BRAIN_FIELDS, "brain_provider": "hermes"})
    assert response.status_code == 303
    assert isinstance(app.state.hub.chat_handler.provider, HermesProvider)
    assert HostConfig.load(state / "host.toml").brain_provider == "hermes"
    assert (state / "host.toml.prev").exists()
    assert app.state.config.brain_provider == "hermes"


def test_an_in_flight_turn_finishes_on_the_brain_it_started_with(state, monkeypatch):
    import asyncio

    from musehost import chat
    from musehost.dashboard import settings
    from musehost.hub import ChatTurn

    app, client, csrf = settings_client(state, monkeypatch)
    hub = app.state.hub
    release = asyncio.Event()

    async def old_brain(turn):
        yield "old "
        await release.wait()
        yield "brain"

    async def new_brain(turn):
        yield "new brain"

    hub.chat_handler = old_brain
    replies = []
    hub.publish = _recording(replies)

    async def scenario():
        first = asyncio.create_task(
            chat.reply(hub, ChatTurn(node_id="n", message_id="m1", text="hi"))
        )
        await asyncio.sleep(0.01)
        monkeypatch.setattr(settings, "build_brain", lambda *a: new_brain)
        settings.apply_config(app, state, app.state.config)  # the swap, mid-turn
        release.set()
        await first
        await chat.reply(hub, ChatTurn(node_id="n", message_id="m2", text="hi"))

    asyncio.run(scenario())
    done = [data["display_text"] for event, data in replies if event == "delta.message_done"]
    assert done == ["old brain", "new brain"]


def _recording(into):
    async def publish(node, event, data):
        into.append((event, data))

    return publish


@pytest.mark.parametrize(
    "field, value",
    [
        ("brain_provider", "skynet"),
        ("brain_effort", "ludicrous"),
        ("brain_timeout_s", "-3"),
        ("brain_timeout_s", "soon"),
        ("brain_base_url", "ftp://x"),
    ],
)
def test_invalid_settings_are_refused_and_nothing_is_written(state, monkeypatch, field, value):
    app, client, csrf = settings_client(state, monkeypatch)
    before = (state / "host.toml").read_text()
    response = post_settings(client, csrf, **{**BRAIN_FIELDS, field: value})
    assert response.status_code == 400
    assert (state / "host.toml").read_text() == before
    assert not (state / "host.toml.prev").exists()


def test_keys_are_write_only_and_saved_privately(state, monkeypatch):
    import os

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # restored afterwards
    app, client, csrf = settings_client(state, monkeypatch)
    secret = "sk-ant-PLANTED-SECRET-123"
    response = client.post(
        "/dashboard/settings",
        data={
            "csrf": csrf,
            "section": "keys",
            "ANTHROPIC_API_KEY": secret,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    env = state / "brain.env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert f"ANTHROPIC_API_KEY={secret}" in env.read_text()
    assert os.environ["ANTHROPIC_API_KEY"] == secret
    page = client.get("/dashboard/settings").text
    assert secret not in page and "PLANTED" not in page
    assert "set" in page

    # An empty field leaves the key alone; "clear" removes it.
    client.post(
        "/dashboard/settings", data={"csrf": csrf, "section": "keys", "ANTHROPIC_API_KEY": ""}
    )
    assert f"ANTHROPIC_API_KEY={secret}" in env.read_text()
    client.post(
        "/dashboard/settings", data={"csrf": csrf, "section": "keys", "clear": "ANTHROPIC_API_KEY"}
    )
    assert "ANTHROPIC_API_KEY=" not in env.read_text()
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_only_downloaded_voices_are_offered(state, monkeypatch):
    voices = state / "models" / "piper"
    voices.mkdir(parents=True)
    (voices / "en_US-lessac-medium.onnx").write_bytes(b"x")
    (voices / "en_GB-alba-medium.onnx").write_bytes(b"x")
    app, client, csrf = settings_client(state, monkeypatch)
    page = client.get("/dashboard/settings").text
    assert "en_GB-alba-medium" in page and "en_US-lessac-medium" in page
    refused = client.post(
        "/dashboard/settings",
        data={
            "csrf": csrf,
            "section": "voice",
            "tts_voice": "de_DE-thorsten-high",
        },
        follow_redirects=False,
    )
    assert refused.status_code == 400


def test_rotating_the_mcp_token_shows_the_new_one_once(state, monkeypatch):
    from musehost import mcp_server

    old = mcp_server.load_token(state)
    app, client, csrf = settings_client(state, monkeypatch)
    page = client.post("/dashboard/settings", data={"csrf": csrf, "section": "mcp-rotate"})
    new = (state / "mcp.token").read_text().strip()
    assert new != old and new in page.text
    assert new not in client.get("/dashboard/settings").text


def test_the_restart_button_answers_then_exits_for_systemd(state, monkeypatch):
    from musehost.dashboard import settings

    exits = []
    monkeypatch.setattr(settings, "_exit_later", lambda code: exits.append(code))
    app, client, csrf = settings_client(state, monkeypatch)
    response = client.post("/dashboard/settings", data={"csrf": csrf, "section": "restart"})
    assert response.status_code == 200 and "restarting" in response.text.lower()
    assert exits == [75]


def test_settings_changes_need_the_csrf_token(state, monkeypatch):
    app, client, _ = settings_client(state, monkeypatch)
    before = (state / "host.toml").read_text()
    response = client.post(
        "/dashboard/settings", data={"section": "brain", **BRAIN_FIELDS}, follow_redirects=False
    )
    assert response.status_code == 403
    assert (state / "host.toml").read_text() == before


def test_a_key_with_characters_brain_env_cant_hold_is_refused(state, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app, client, csrf = settings_client(state, monkeypatch)
    for bad in ('sk-"quoted"', "sk-two words", "sk-back\\slash", "sk-new\nLINE=x"):
        response = client.post(
            "/dashboard/settings",
            data={
                "csrf": csrf,
                "section": "keys",
                "OPENAI_API_KEY": bad,
            },
            follow_redirects=False,
        )
        assert response.status_code == 400
    assert not (state / "brain.env").exists() or "OPENAI" not in (state / "brain.env").read_text()
