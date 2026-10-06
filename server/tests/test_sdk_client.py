"""The gadget SDK's own client code against a running musehost.

These use ``musegadget`` from ../muse-gadget-sdk-selfhost/linux (branch ``self-host``),
so a response shape the SDK can't parse fails here, not on a device.
"""

import asyncio
import json
import time

import pytest
from conftest import serving
from musegadget import muse_api, tls
from musegadget.executor import Account, Executor
from musegadget.identity import Identity
from musegadget.service import Service

from musehost.cli import main
from musehost.config import HostConfig

# The SDK's own derivation of node ids: homelink- plus the last 3 MAC bytes.
MAC = "02:00:00:ab:cd:ef"
NODE_ID = "homelink-abcdef"


@pytest.fixture
def pairing(state) -> dict:
    out = state.parent / "pairing.json"
    assert main(["--state-dir", str(state), "enroll", NODE_ID, "--out", str(out)]) == 0
    return json.loads(out.read_text())


@pytest.fixture
def config(state) -> HostConfig:
    return HostConfig.load(state / "host.toml")


def test_the_sdk_node_id_matches_what_the_host_accepts():
    assert Identity(MAC).node_id == NODE_ID


def test_the_sdk_fetches_vms_through_the_provisioned_ca(state, pairing, config):
    context = tls.context_for(pairing["ca_cert"])
    with serving(state):
        vms, status = muse_api.fetch_vms_with_status(
            pairing["access_token"], muse_api.api_root(pairing["api_url_v2"]), context=context
        )
    assert status == 200
    [vm] = vms
    assert vm["vm_url"] == f"wss://{config.public_host}/"
    assert (vm["vm_id"], vm["vm_name"], vm["is_default"]) == ("home", "home", True)
    assert vm["vm_auth_token"]


def test_the_sdk_rejects_the_host_without_the_provisioned_ca(state, pairing):
    with serving(state):
        assert muse_api.fetch_vms_with_status(
            pairing["access_token"], muse_api.api_root(pairing["api_url_v2"])
        ) == ([], None)


def test_the_sdk_refreshes_and_a_lost_response_retry_still_works(state, pairing):
    context = tls.context_for(pairing["ca_cert"])
    root = muse_api.api_root(pairing["api_url_v2"])
    with serving(state):
        first, status = muse_api.refresh_device_token(
            pairing["refresh_token"], NODE_ID, root, sdk_token="mgst_ignored", context=context
        )
        assert status == 200 and first["access_token"] and first["refresh_token"]
        retry, status = muse_api.refresh_device_token(
            pairing["refresh_token"], NODE_ID, root, context=context
        )
        assert status == 200
        vms, status = muse_api.fetch_vms_with_status(retry["access_token"], root, context=context)
        assert (len(vms), status) == (1, 200)


def test_the_sdk_service_rotates_an_enrolled_pairing(state, pairing, monkeypatch):
    sdk_state = state.parent / "musegadget"
    sdk_state.mkdir()
    monkeypatch.setenv("MUSEGADGET_STATE_DIR", str(sdk_state))
    stale = {**pairing, "access_token_saved_at": 0}
    (sdk_state / "pairing.json").write_text(json.dumps(stale))

    async def refresh():
        service = Service(identity=Identity(MAC), executor=Executor(Account.current()))
        return await service._maybe_refresh(stale)

    with serving(state):
        rotated = asyncio.run(refresh())
    assert rotated["access_token"] != pairing["access_token"]
    saved = json.loads((sdk_state / "pairing.json").read_text())
    assert saved["access_token"] == rotated["access_token"]
    assert saved["access_token_saved_at"] >= int(time.time()) - 5
