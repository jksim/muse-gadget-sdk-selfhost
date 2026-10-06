import pytest
from starlette.testclient import TestClient

from musehost import pki
from musehost.app import create_app
from musehost.tokens import ACCESS_TTL_S, Tokens


class Clock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(state, clock):
    return create_app(state, clock=clock)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def tokens(app) -> Tokens:
    return app.state.tokens


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-API-Version": "1.0.0"}


def test_fetch_vms_lists_the_single_vm_with_a_fresh_vm_token(client, tokens, app):
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    response = client.get("/fetch_vms", headers=bearer(access))
    assert response.status_code == 200
    [vm] = response.json()["vm_list"]
    config = app.state.config
    assert vm["vm_id"] == vm["vm_name"] == "home"
    assert vm["vm_ws_url"] == f"wss://{config.public_host}/"
    assert vm["default"] is True
    assert tokens.verify_vm_token(vm["vm_auth_token"], "home") == "homelink-abcdef"


def test_each_fetch_mints_a_new_vm_token(client, tokens):
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    first = client.get("/fetch_vms", headers=bearer(access)).json()["vm_list"][0]
    second = client.get("/fetch_vms", headers=bearer(access)).json()["vm_list"][0]
    assert first["vm_auth_token"] != second["vm_auth_token"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "x"}])
def test_fetch_vms_without_a_valid_access_token_is_401(client, headers):
    assert client.get("/fetch_vms", headers=headers).status_code == 401


def test_fetch_vms_with_an_expired_access_token_is_401(client, tokens, clock):
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    clock.now += ACCESS_TTL_S
    assert client.get("/fetch_vms", headers=bearer(access)).status_code == 401


def test_fetch_vms_for_a_revoked_device_is_401(client, tokens):
    access, _ = tokens.enroll("homelink-abcdef", "pi")
    tokens.revoke("homelink-abcdef")
    assert client.get("/fetch_vms", headers=bearer(access)).status_code == 401


def test_fetch_vms_with_a_refresh_token_is_401(client, tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    assert client.get("/fetch_vms", headers=bearer(refresh)).status_code == 401


def test_the_noise_public_key_is_available_to_the_app(app, state):
    assert app.state.noise_static_pub == pki.noise_public_b64(
        pki.load_noise_key(state / "noise_static.key")
    )


def refresh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer hatch_refresh:{token}"}


def test_refresh_returns_a_new_pair_at_the_top_level(client, tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    response = client.post(
        "/device_token/refresh",
        headers=refresh_headers(refresh),
        json={"device_id": "homelink-abcdef", "sdk_token": "mgst_ignored"},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "refresh_token"}
    assert tokens.device_for_access(body["access_token"]) == "homelink-abcdef"


def test_a_lost_response_refresh_retry_is_200(client, tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    body = {"device_id": "homelink-abcdef"}
    assert client.post("/device_token/refresh", headers=refresh_headers(refresh), json=body)
    retry = client.post("/device_token/refresh", headers=refresh_headers(refresh), json=body)
    assert retry.status_code == 200


@pytest.mark.parametrize(
    "auth",
    ["Bearer {refresh}", "Bearer hatch_refresh:nope", "Bearer hatch_refresh:{access}", ""],
)
def test_refresh_without_a_valid_prefixed_refresh_token_is_401(client, tokens, auth):
    access, refresh = tokens.enroll("homelink-abcdef", "pi")
    headers = {"Authorization": auth.format(refresh=refresh, access=access)} if auth else {}
    response = client.post(
        "/device_token/refresh", headers=headers, json={"device_id": "homelink-abcdef"}
    )
    assert response.status_code == 401


def test_refresh_for_a_different_device_id_is_401(client, tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    response = client.post(
        "/device_token/refresh",
        headers=refresh_headers(refresh),
        json={"device_id": "homelink-123456"},
    )
    assert response.status_code == 401


def test_refresh_for_a_revoked_device_is_401(client, tokens):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    tokens.revoke("homelink-abcdef")
    response = client.post(
        "/device_token/refresh",
        headers=refresh_headers(refresh),
        json={"device_id": "homelink-abcdef"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}"])
def test_refresh_with_a_malformed_body_is_400(client, tokens, body):
    _, refresh = tokens.enroll("homelink-abcdef", "pi")
    response = client.post("/device_token/refresh", headers=refresh_headers(refresh), content=body)
    assert response.status_code == 400


def test_after_revoke_fetch_and_refresh_are_401(client, tokens):
    access, refresh = tokens.enroll("homelink-abcdef", "pi")
    tokens.revoke("homelink-abcdef")
    assert client.get("/fetch_vms", headers=bearer(access)).status_code == 401
    response = client.post(
        "/device_token/refresh",
        headers=refresh_headers(refresh),
        json={"device_id": "homelink-abcdef"},
    )
    assert response.status_code == 401


BUNDLE_KEYS = {
    "access_token",
    "refresh_token",
    "token_type",
    "api_url_v2",
    "noise_host",
    "ca_cert",
    "noise_static_pub",
}


def test_enroll_swaps_a_grant_for_a_working_provisioning_bundle(client, tokens, app):
    code = tokens.create_grant()
    response = client.post(
        "/v1/enroll", json={"grant": code, "node_id": "homelink-abcdef", "display_name": "pi"}
    )
    assert response.status_code == 200
    bundle = response.json()
    assert set(bundle) == BUNDLE_KEYS
    config = app.state.config
    assert (bundle["token_type"], bundle["api_url_v2"], bundle["noise_host"]) == (
        "device",
        config.api_url,
        config.public_host,
    )
    assert bundle["ca_cert"] == app.state.ca_pem
    assert bundle["noise_static_pub"] == app.state.noise_static_pub
    assert client.get("/fetch_vms", headers=bearer(bundle["access_token"])).status_code == 200


def test_enroll_with_a_used_grant_is_401(client, tokens):
    code = tokens.create_grant()
    body = {"grant": code, "node_id": "homelink-abcdef"}
    assert client.post("/v1/enroll", json=body).status_code == 200
    assert client.post("/v1/enroll", json=body).status_code == 401


def test_enroll_with_an_expired_grant_is_401(client, tokens, clock):
    code = tokens.create_grant()
    clock.now += 600
    response = client.post("/v1/enroll", json={"grant": code, "node_id": "homelink-abcdef"})
    assert response.status_code == 401


def test_enroll_with_an_unknown_grant_is_401(client):
    response = client.post("/v1/enroll", json={"grant": "nope", "node_id": "homelink-abcdef"})
    assert response.status_code == 401


def test_enroll_with_a_malformed_node_id_is_400_and_keeps_the_grant(client, tokens):
    code = tokens.create_grant()
    assert client.post("/v1/enroll", json={"grant": code, "node_id": "pi"}).status_code == 400
    response = client.post("/v1/enroll", json={"grant": code, "node_id": "homelink-abcdef"})
    assert response.status_code == 200


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"node_id": "homelink-abcdef"}'])
def test_enroll_with_a_malformed_body_is_400(client, body):
    assert client.post("/v1/enroll", content=body).status_code == 400


def test_on_port_443_fetch_vms_advertises_a_bare_wss_url(tmp_path):
    from musehost.cli import main

    state = tmp_path / "state443"
    assert (
        main(["--state-dir", str(state), "init", "--hostname", "localhost", "--port", "443"]) == 0
    )
    app = create_app(state)
    access, _ = app.state.tokens.enroll("homelink-abcdef", "pi")
    [vm] = TestClient(app).get("/fetch_vms", headers=bearer(access)).json()["vm_list"]
    assert vm["vm_ws_url"] == "wss://localhost/"
