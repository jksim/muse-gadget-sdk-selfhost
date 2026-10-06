"""What a gadget is told to reach this host, as ``provision_v2`` fields."""

from __future__ import annotations

from musehost.config import HostConfig


def bundle(
    config: HostConfig, ca_pem: str, noise_static_pub: str, access: str, refresh: str
) -> dict:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "device",
        "api_url_v2": config.api_url,
        "noise_host": config.public_host,
        "ca_cert": ca_pem,
        "noise_static_pub": noise_static_pub,
    }
