# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import io
import json

import pytest
from certs import https_json_server, make_pki

from musegadget import muse_api, tls

TOKENS = {"access_token": "new-a", "refresh_token": "new-r"}


@pytest.fixture
def sent(monkeypatch):
    bodies: list[dict] = []

    def urlopen(req, timeout):
        bodies.append((req.get_header("Authorization"), json.loads(req.data)))
        return io.BytesIO(json.dumps(TOKENS).encode())

    monkeypatch.setattr(muse_api.urllib.request, "urlopen", urlopen)
    return bodies


def test_refresh_sends_only_the_device_id_without_a_key(sent):
    assert muse_api.refresh_device_token("r", "homelink-abcdef") == (TOKENS, 200)
    assert sent == [("Bearer hatch_refresh:r", {"device_id": "homelink-abcdef"})]


def test_refresh_sends_the_sdk_token(sent):
    muse_api.refresh_device_token("r", "homelink-abcdef", sdk_token="mgst_token")
    assert sent == [("Bearer hatch_refresh:r", {"device_id": "homelink-abcdef", "sdk_token": "mgst_token"})]



def test_refresh_never_presents_the_access_token_or_doubles_the_prefix(sent):
    for stored in ("hatch_refresh:raw-r", "raw-r"):
        sent.clear()
        assert muse_api.refresh_device_token(stored, "homelink-abcdef") == (TOKENS, 200)
        assert [header for header, _ in sent] == ["Bearer hatch_refresh:raw-r"]


def test_a_rejected_refresh_reports_401(monkeypatch):
    def urlopen(req, timeout):
        raise muse_api.urllib.error.HTTPError(req.full_url, 401, "", {}, io.BytesIO(b""))

    monkeypatch.setattr(muse_api.urllib.request, "urlopen", urlopen)
    assert muse_api.refresh_device_token("r", "homelink-abcdef") == (None, 401)


VM_LIST = {"vm_list": [{"vm_id": "home", "vm_name": "home", "vm_ws_url": "wss://h/",
                        "vm_auth_token": "vm-t", "default": True}]}


class Reply(io.BytesIO):
    def __init__(self, body: dict) -> None:
        super().__init__(json.dumps(body).encode())

    def getcode(self) -> int:
        return 200


def test_a_tls_context_reaches_urlopen_for_fetch_and_refresh(monkeypatch):
    contexts = []

    def urlopen(req, timeout, context=None):
        contexts.append(context)
        return Reply(VM_LIST if req.get_method() == "GET" else TOKENS)

    monkeypatch.setattr(muse_api.urllib.request, "urlopen", urlopen)
    context = tls.context_for(make_pki().ca_pem)
    vms, status = muse_api.fetch_vms_with_status("a", context=context)
    assert (len(vms), status) == (1, 200)
    assert muse_api.refresh_device_token("r", "homelink-abcdef", context=context) == (TOKENS, 200)
    assert contexts == [context, context]


def test_fetch_without_a_context_calls_urlopen_as_before(monkeypatch):
    def urlopen(req, timeout):
        return Reply(VM_LIST)

    monkeypatch.setattr(muse_api.urllib.request, "urlopen", urlopen)
    vms, status = muse_api.fetch_vms_with_status("a")
    assert (len(vms), status) == (1, 200)


def test_a_host_with_a_private_ca_is_trusted_only_through_that_ca(tmp_path):
    pki = make_pki()
    with https_json_server(pki, VM_LIST, tmp_path) as root:
        trusted, status = muse_api.fetch_vms_with_status("a", root, context=tls.context_for(pki.ca_pem))
        assert (len(trusted), status) == (1, 200)
        assert muse_api.fetch_vms_with_status("a", root) == ([], None)
        other_ca = tls.context_for(make_pki("other CA").ca_pem)
        assert muse_api.fetch_vms_with_status("a", root, context=other_ca) == ([], None)
