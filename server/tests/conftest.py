import socket
import ssl
import threading
import time
from contextlib import contextmanager

import pytest

from musehost.cli import main, make_server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def state(tmp_path):
    """Host state for localhost/127.0.0.1 on a free port."""
    path = tmp_path / "state"
    port = free_port()
    assert (
        main(
            [
                "--state-dir",
                str(path),
                "init",
                "--hostname",
                "localhost",
                "--ip",
                "127.0.0.1",
                "--port",
                str(port),
            ]
        )
        == 0
    )
    # MCP (127.0.0.1:8765 by default) stays off unless a test turns it on.
    import dataclasses

    from musehost.config import HostConfig

    dataclasses.replace(HostConfig.load(path / "host.toml"), mcp_port=0).save(path / "host.toml")
    return path


def ca_context(state) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(state / "ca.pem"))


@contextmanager
def serving(state, port: int | None = None):
    """Run ``musehost serve`` for ``state`` in a thread; yields the server."""
    server = make_server(state, port=port, bind="127.0.0.1")
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("musehost serve did not start")
        time.sleep(0.02)
    try:
        yield server
    finally:
        server.should_exit = True
        thread.join(10)


@pytest.fixture
def live(state):
    """A running host on ``state`` plus helpers to mint VM bearers."""
    import tomllib
    import types

    from musehost.store import Store
    from musehost.tokens import Tokens

    port = tomllib.loads((state / "host.toml").read_text())["port"]
    tokens = Tokens(Store.open(state / "musehost.db"))

    def vm_token(node_id: str = "homelink-abcdef", vm_id: str = "home") -> str:
        if not tokens.store.db.execute(
            "SELECT 1 FROM devices WHERE node_id = ?", (node_id,)
        ).fetchone():
            tokens.enroll(node_id, "test gadget")
        return tokens.issue_vm_token(node_id, vm_id)

    with serving(state) as server:
        yield types.SimpleNamespace(
            port=port,
            ca=state / "ca.pem",
            tokens=tokens,
            vm_token=vm_token,
            server=server,
            app=server.config.app,
        )


@pytest.fixture(autouse=True)
def short_admin_socket(monkeypatch):
    """AF_UNIX paths are limited to ~107 bytes; pytest's tmp paths can get close."""
    import shutil
    import tempfile

    directory = tempfile.mkdtemp(dir="/tmp", prefix="mh-")
    monkeypatch.setenv("MUSEHOST_ADMIN_SOCKET", f"{directory}/admin.sock")
    yield
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_real_llm_keys(monkeypatch):
    """Tests never reach a real model: clear provider keys (tests set fakes as needed)."""
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "VLLM_API_KEY",
        "HERMES_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
