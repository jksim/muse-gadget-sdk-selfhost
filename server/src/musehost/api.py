"""REST endpoints the gadget calls with its device token."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from musehost import provisioning
from musehost.tokens import NODE_ID_RE


def bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def unauthorized() -> JSONResponse:
    return JSONResponse({"error_title": "unauthorized"}, status_code=401)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def fetch_vms(request: Request) -> JSONResponse:
    tokens = request.app.state.tokens
    node_id = tokens.device_for_access(bearer(request))
    if node_id is None:
        return unauthorized()
    config = request.app.state.config
    return JSONResponse(
        {
            "vm_list": [
                {
                    "vm_id": config.vm_id,
                    "vm_name": config.vm_id,
                    "vm_ws_url": f"wss://{config.public_host}/",
                    "vm_auth_token": tokens.issue_vm_token(node_id, config.vm_id),
                    "default": True,
                }
            ]
        }
    )


REFRESH_PREFIX = "hatch_refresh:"


async def refresh_device_token(request: Request) -> JSONResponse:
    body = await json_body(request)
    device_id = body.get("device_id") if body else None
    if not isinstance(device_id, str) or not device_id:
        return bad_request()
    token = bearer(request)
    if not token.startswith(REFRESH_PREFIX):
        return unauthorized()
    pair = request.app.state.tokens.refresh(token.removeprefix(REFRESH_PREFIX), device_id)
    if pair is None:
        return unauthorized()
    access, refresh = pair
    return JSONResponse({"access_token": access, "refresh_token": refresh})


def bad_request() -> JSONResponse:
    return JSONResponse({"error_title": "bad request"}, status_code=400)


async def json_body(request: Request) -> dict | None:
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


async def enroll(request: Request) -> JSONResponse:
    """Swap a one-time grant from ``musehost grant`` for a provisioning bundle."""
    body = await json_body(request)
    if body is None:
        return bad_request()
    grant, node_id = body.get("grant"), body.get("node_id")
    display_name = body.get("display_name") or ""
    if not isinstance(grant, str) or not isinstance(display_name, str):
        return bad_request()
    if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
        return bad_request()
    state = request.app.state
    if not state.tokens.redeem_grant(grant):
        return unauthorized()
    access, refresh = state.tokens.enroll(node_id, display_name[:64])
    return JSONResponse(
        provisioning.bundle(state.config, state.ca_pem, state.noise_static_pub, access, refresh)
    )


routes = [
    Route("/healthz", healthz),
    Route("/fetch_vms", fetch_vms),
    Route("/device_token/refresh", refresh_device_token, methods=["POST"]),
    Route("/v1/enroll", enroll, methods=["POST"]),
]
