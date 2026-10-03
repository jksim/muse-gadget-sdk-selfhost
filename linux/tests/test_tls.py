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

import ssl

import pytest
from certs import make_pki

from musegadget import tls


def test_no_provisioned_ca_means_the_system_store():
    assert tls.context_for(None) is None
    assert tls.context_for("") is None


def test_a_provisioned_ca_is_the_only_trust_anchor():
    context = tls.context_for(make_pki().ca_pem)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert len(context.get_ca_certs()) == 1


def test_a_ca_that_is_not_a_certificate_is_rejected():
    with pytest.raises(ValueError):
        tls.context_for("-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n")
    with pytest.raises(ValueError):
        tls.context_for("hello")


def test_a_noise_static_key_pin_is_32_bytes_of_unpadded_base64url():
    import base64

    raw = bytes(range(32))
    assert tls.parse_static_key(base64.urlsafe_b64encode(raw).rstrip(b"=").decode()) == raw


@pytest.mark.parametrize("text", [
    "A" * 42,                         # 31 bytes
    "A" * 44,                         # 33 bytes
    "A" * 43 + "=",                   # padded
    "+" * 43,                         # standard base64 alphabet
    "",
])
def test_a_malformed_noise_static_key_pin_is_rejected(text):
    with pytest.raises(ValueError):
        tls.parse_static_key(text)
