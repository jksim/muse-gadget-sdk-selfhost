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

import json

from certs import make_pki

from musegadget import cli
from musegadget.ble_setup import Credentials

VMS = [{"vm_url": "wss://h/", "vm_auth_token": "t", "vm_name": "home", "vm_id": "home",
        "is_default": True}]
RECORD_KEYS = {"access_token", "refresh_token", "token_type", "username", "api_url",
               "api_url_v2", "noise_host", "access_token_saved_at"}


def credentials(**trust) -> Credentials:
    return Credentials(access_token="a", refresh_token="r", username="", api_url="",
                       api_url_v2="https://musehost.local:8443", noise_host="musehost.local:8443",
                       **trust)


def verify_and_save(creds, tmp_path, monkeypatch) -> tuple[list, dict]:
    monkeypatch.setenv("MUSEGADGET_STATE_DIR", str(tmp_path))
    calls = []

    def fetch(access_token, root, **kwargs):
        calls.append(kwargs)
        return VMS, 200

    monkeypatch.setattr(cli.muse_api, "fetch_vms_with_status", fetch)
    cli._verify_and_save(creds, lambda save: save())
    return calls, json.loads((tmp_path / "pairing.json").read_text())


def test_pairing_checks_the_token_through_the_provisioned_ca_and_stores_trust(
    tmp_path, monkeypatch,
):
    ca = make_pki().ca_pem
    calls, record = verify_and_save(credentials(ca_cert=ca, noise_static_pub="pin"),
                                    tmp_path, monkeypatch)
    assert len(calls[0]["context"].get_ca_certs()) == 1
    assert (record["ca_cert"], record["noise_static_pub"]) == (ca, "pin")


def test_pairing_without_trust_fields_saves_the_usual_record(tmp_path, monkeypatch):
    calls, record = verify_and_save(credentials(), tmp_path, monkeypatch)
    assert calls == [{}]
    assert set(record) == RECORD_KEYS
