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
"""

from __future__ import annotations

import ssl


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
