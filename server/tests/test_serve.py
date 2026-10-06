import http.client
import json
import logging
import tomllib

import pytest
from conftest import ca_context, free_port, serving
from starlette.testclient import TestClient

from musehost.app import create_app


def test_healthz_answers_ok(state):
    app = create_app(state)
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_serve_answers_over_tls_on_the_configured_port(state):
    port = tomllib.loads((state / "host.toml").read_text())["port"]
    with serving(state):
        conn = http.client.HTTPSConnection("localhost", port, context=ca_context(state), timeout=5)
        conn.request("GET", "/healthz")
        response = conn.getresponse()
        assert (response.status, json.loads(response.read())) == (200, {"ok": True})
        conn.close()


def test_serve_refuses_plain_http(state):
    port = tomllib.loads((state / "host.toml").read_text())["port"]
    with serving(state):
        conn = http.client.HTTPConnection("localhost", port, timeout=5)
        with pytest.raises((ConnectionError, http.client.HTTPException)):
            conn.request("GET", "/healthz")
            conn.getresponse()


def test_a_port_override_is_used_and_warned_about(state, caplog):
    override = free_port()
    with caplog.at_level(logging.WARNING, logger="musehost"), serving(state, port=override):
        conn = http.client.HTTPSConnection(
            "localhost", override, context=ca_context(state), timeout=5
        )
        conn.request("GET", "/healthz")
        assert conn.getresponse().status == 200
        conn.close()
    assert any("differs from" in record.message for record in caplog.records)
