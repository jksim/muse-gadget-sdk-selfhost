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

"""Trust settings for a gadget paired with a self-hosted Muse.

Pairing may provision ``ca_cert``, the PEM of the host's own CA. It then
replaces the system store rather than adding to it, so the gadget trusts
nothing else for its host, and hostname checks stay on. Without it the
system store is used exactly as before.

Pairing may also provision ``noise_static_pub``, the host's Noise static
public key, which the link then requires the responder to present.
"""

from __future__ import annotations

import ssl

from musegadget.pairing import b64url_decode

NOISE_KEY_BYTES = 32


def context_for(ca_pem: str | None) -> ssl.SSLContext | None:
    """The TLS context for a provisioned CA, or None for the system store.

    Raises ValueError when ``ca_pem`` holds no usable certificate.
    """
    if not ca_pem:
        return None
    try:
        return ssl.create_default_context(cadata=ca_pem)
    except (ssl.SSLError, ValueError) as exc:
        raise ValueError(f"invalid CA certificate: {exc}") from None


def parse_static_key(text: str) -> bytes:
    """The 32-byte X25519 key in a ``noise_static_pub`` pin.

    Raises ValueError unless ``text`` is exactly 32 bytes of unpadded base64url.
    """
    try:
        key = b64url_decode(text)
    except ValueError:
        raise ValueError("invalid Noise static key: not unpadded base64url") from None
    if len(key) != NOISE_KEY_BYTES:
        raise ValueError(f"invalid Noise static key: {len(key)} bytes, expected 32")
    return key
