"""The admin socket and the CLI commands that use it, against a live host."""

import json

from test_link import with_device

from musehost.cli import main


def cli(state, *args) -> int:
    return main(["--state-dir", str(state), *args])


def test_invoke_prints_the_devices_result(live, state, capsys):
    def run_command(command, params, timeout_ms):
        return {"ok": True, "payload": {"healthy": True, "params": params}}

    code = with_device(
        live,
        run_command,
        lambda: cli(state, "invoke", "homelink-abcdef", "device.health", '{"a":1}'),
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"ok": True, "payload": {"healthy": True, "params": {"a": 1}}, "error": None}


def test_a_device_error_exits_1(live, state, capsys):
    code = with_device(
        live,
        lambda *a: {"ok": False, "error": "no such command"},
        lambda: cli(state, "invoke", "homelink-abcdef", "nope"),
    )
    assert code == 1
    assert "no such command" in capsys.readouterr().out


def test_an_offline_device_exits_2(live, state, capsys):
    assert cli(state, "invoke", "homelink-123456", "device.health") == 2
    assert "offline" in capsys.readouterr().err


def test_bad_json_params_exit_2_without_contacting_anything(state, capsys):
    assert cli(state, "invoke", "homelink-abcdef", "device.health", "{not json") == 2
    assert "JSON" in capsys.readouterr().err


def test_invoke_with_the_server_down_says_so(state, capsys):
    assert cli(state, "invoke", "homelink-abcdef", "device.health") == 2
    assert "not running" in capsys.readouterr().err


def test_devices_list_shows_online_status(live, state, capsys):
    def listing():
        cli(state, "devices", "list")
        return capsys.readouterr().out

    out = with_device(live, lambda *a: {"ok": True}, listing)
    row = next(line for line in out.splitlines() if "homelink-abcdef" in line)
    assert "ONLINE" in out and row.split()[-1] == "yes"


def test_devices_list_marks_online_unknown_when_the_server_is_down(state, capsys):
    from musehost.store import Store
    from musehost.tokens import Tokens

    Tokens(Store.open(state / "musehost.db")).enroll("homelink-abcdef", "pi")
    cli(state, "devices", "list")
    row = next(line for line in capsys.readouterr().out.splitlines() if "homelink-abcdef" in line)
    assert row.split()[-1] == "?"


def test_the_admin_socket_is_private(live):
    import os
    import stat

    path = os.environ["MUSEHOST_ADMIN_SOCKET"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_revoke_with_the_server_down_still_revokes_and_says_so(state, capsys):
    from musehost.store import Store
    from musehost.tokens import Tokens

    tokens = Tokens(Store.open(state / "musehost.db"))
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    assert cli(state, "devices", "revoke", "homelink-abcdef") == 0
    assert "wasn't notified" in capsys.readouterr().out
    assert tokens.device_for_access(access) is None


# -- musehost chat ------------------------------------------------------------------------


def install_brain(live, provider):
    from musehost.brain import Brain
    from musehost.brain.history import History

    app = live.app
    brain = Brain(app.state.hub, provider, app.state.config, History(app.state.tokens.store))
    app.state.loop.call_soon_threadsafe(app.state.hub.set_chat_handler, brain)
    return brain


def test_chat_streams_clios_reply_to_the_terminal(live, state, capsys, monkeypatch):
    from test_brain import FakeProvider

    from musehost.brain.providers.base import Done, Text

    install_brain(live, FakeProvider(Text("Hello "), Text("operator."), Done("end_turn", {})))
    lines = iter(["hi Clio"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    assert cli(state, "chat") == 0
    assert "Hello operator." in capsys.readouterr().out


def test_chat_with_a_device_gets_that_devices_tools(live, state, capsys, monkeypatch):
    from test_brain_tools import ToolCallingProvider
    from test_link import with_device

    from musehost.brain.providers.base import ToolCall

    provider = ToolCallingProvider(ToolCall("t1", "device_health", {}))
    install_brain(live, provider)
    lines = iter(["how's the battery?"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))

    def run_command(command, params, timeout_ms):
        return {"ok": True, "payload": {"battery_pct": 77}}

    code = with_device(
        live,
        run_command,
        lambda: cli(state, "chat", "--device", "homelink-abcdef"),
    )
    assert code == 0
    assert {s.name for s in provider.tools_offered} == {"device_health"}
    assert '"battery_pct": 77' in provider.results[0].content


def test_chat_keeps_its_own_conversation_and_new_starts_afresh(live, state, monkeypatch):
    from test_brain import FakeProvider

    provider = FakeProvider()
    install_brain(live, provider)
    for args in (("chat",), ("chat",), ("chat", "--new")):
        lines = iter(["hello"])
        monkeypatch.setattr("builtins.input", lambda prompt="", lines=lines: next(lines))
        cli(state, *args)
    first, second, third = (len(call["history"]) for call in provider.calls)
    assert (first, second, third) == (0, 2, 0)


def test_chat_without_a_brain_gets_the_placeholder(live, state, capsys, monkeypatch):
    lines = iter(["hello"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    assert cli(state, "chat") == 0
    assert "brain isn't connected" in capsys.readouterr().out


def test_chat_with_the_server_down_says_so(state, capsys, monkeypatch):
    lines = iter(["hello"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    assert cli(state, "chat") == 2
    assert "not running" in capsys.readouterr().err
