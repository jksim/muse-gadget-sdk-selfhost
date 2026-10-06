"""The host's single ASGI app: the device REST API and the Noise WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute

from musehost import admin, api, chat, link, noise_server, pki, speech, voice_out
from musehost.brain import Brain
from musehost.brain.history import History
from musehost.config import CONFIG_FILE, HostConfig
from musehost.hub import Hub
from musehost.store import Store
from musehost.tokens import Tokens

DB_FILE = "musehost.db"
log = logging.getLogger(__name__)


def build_brain(config: HostConfig, hub: Hub, store: Store | None) -> Brain | None:
    """Clio's brain as ``host.toml`` and the environment configure it, else None.

    None leaves the placeholder answering: the brain is off, or its provider
    lacks a key or model. Keys come from the environment (``brain.env``).
    """
    provider_name = config.brain_provider
    if not provider_name:
        return None
    if provider_name == "claude":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            log.warning("brain: ANTHROPIC_API_KEY is not set; Clio uses the placeholder")
            return None
        from musehost.brain.providers.claude import ClaudeProvider

        provider = ClaudeProvider(
            api_key=key,
            model=config.brain_model,
            effort=config.brain_effort,
            max_tokens=config.brain_max_tokens,
            web_search=config.brain_web_search,
        )
    elif provider_name in ("openai", "vllm"):
        from musehost.brain.providers.openai_compat import OpenAICompatProvider

        key_name = "OPENAI_API_KEY" if provider_name == "openai" else "VLLM_API_KEY"
        key = os.environ.get(key_name)
        missing = [
            what
            for what, ok in (
                ("brain_model", config.brain_model),
                (key_name, key or provider_name == "vllm"),
                ("brain_base_url", config.brain_base_url or provider_name == "openai"),
            )
            if not ok
        ]
        if missing:
            log.warning(
                "brain: %s needs %s; Clio uses the placeholder", provider_name, ", ".join(missing)
            )
            return None
        provider = OpenAICompatProvider(
            name=provider_name,
            model=config.brain_model,
            api_key=key,
            base_url=config.brain_base_url or None,
            max_tokens=config.brain_max_tokens,
        )
    else:
        log.warning("brain: unknown provider %r; Clio uses the placeholder", provider_name)
        return None
    history = History(store, idle_minutes=config.brain_idle_minutes) if store else None
    log.info("brain: %s with %s", provider.name, provider.model)
    return Brain(hub, provider, config, history=history)


def create_app(state: Path, clock: Callable[[], float] = time.time) -> Starlette:
    """The app for the host state in ``state``; ``clock`` is injectable for tests."""
    routes = [*api.routes, WebSocketRoute("/v1/noise", noise_server.noise_endpoint)]

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # The CLI's admin socket and tests reach the hub on this loop.
        app.state.loop = asyncio.get_running_loop()
        server = await admin.serve(admin.socket_path(state), app.state.hub)
        app.state.transcriber.start()  # loads the model in the background
        app.state.synthesizer.start()  # loads the voice in the background
        try:
            yield
        finally:
            server.close()
            app.state.transcriber.close()
            app.state.synthesizer.close()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.config = HostConfig.load(state / CONFIG_FILE)
    app.state.tokens = Tokens(Store.open(state / DB_FILE), clock=clock)
    app.state.ca_pem = (state / "ca.pem").read_text()
    app.state.noise_key = pki.load_noise_key(state / "noise_static.key")
    app.state.noise_static_pub = pki.noise_public_b64(app.state.noise_key)
    app.state.hub = Hub()

    def unpair_on_revoke(node_id: str) -> None:
        # Revocations made in this process (e.g. refresh-token reuse) end the
        # device's live sessions too; may be called from any thread.
        loop = getattr(app.state, "loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(lambda: loop.create_task(app.state.hub.unpair(node_id)))

    app.state.tokens.on_revoke = unpair_on_revoke
    app.state.voice_dir = state / "voice"
    app.state.transcriber = speech.Transcriber(
        speech.engine_for(app.state.config, state / "models"),
        timeout_s=app.state.config.speech_timeout_s,
    )
    app.state.synthesizer = voice_out.Synthesizer(
        voice_out.engine_for(app.state.config, state / "models")
    )
    brain = build_brain(app.state.config, app.state.hub, app.state.tokens.store)
    if brain is not None:
        app.state.hub.set_chat_handler(brain)
    app.state.stream_handlers = {
        "/identity": noise_server.identity,
        "/link-control": link.link_control,
        "/link-tunnel": link.link_tunnel,
        "/chat/subscribe": chat.chat_subscribe,
        "/chat/stream": chat.chat_stream,
        "/tts": chat.tts,
    }
    return app
