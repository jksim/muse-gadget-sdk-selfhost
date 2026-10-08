"""The settings page: brain, keys, voice, MCP token, restart.

Brain fields, keys and the voice apply at once; ``mcp_port`` and
``speech_model`` after a restart. ``host.toml`` is replaced atomically, the
previous copy kept as ``host.toml.prev``. Keys are write-only: the page says
whether one is set, never what it is.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import shutil
from pathlib import Path

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.routing import Route

from musehost import mcp_server, voice_out
from musehost.app import build_brain
from musehost.config import HostConfig
from musehost.dashboard.web import KEY_FOR, page, require_csrf

log = logging.getLogger(__name__)

CONFIG_FILE = "host.toml"
ENV_FILE = "brain.env"
PROVIDERS = ("claude", "openai", "vllm", "hermes", "")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
KEYS = tuple(KEY_FOR.values())
RESTART_EXIT = 75  # non-zero, so systemd's Restart=on-failure brings musehost back
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._~+/=:-]{1,512}$")  # safe in a systemd EnvironmentFile
_MODEL_NAME = re.compile(r"^[A-Za-z0-9._/:-]{0,128}$")


class Invalid(ValueError):
    pass


def _exit_later(code: int) -> None:
    """Exit after the response has gone out; systemd restarts musehost."""
    asyncio.get_running_loop().call_later(1.0, os._exit, code)


def voices(state: Path) -> list[str]:
    return sorted(p.stem for p in (state / "models" / "piper").glob("*.onnx"))


def save_config(state: Path, config: HostConfig) -> None:
    path = state / CONFIG_FILE
    tmp = path.with_suffix(".tmp")
    config.save(tmp)
    if path.exists():
        shutil.copy2(path, state / f"{CONFIG_FILE}.prev")
    os.replace(tmp, path)


def apply_config(app, state: Path, config: HostConfig) -> None:
    """Make ``config`` live: new turns use the new brain; turns under way keep theirs."""
    app.state.config = config
    app.state.hub.chat_handler = build_brain(config, app.state.hub, app.state.tokens.store)


def _brain_config(config: HostConfig, form) -> HostConfig:
    provider = str(form.get("brain_provider", ""))
    if provider not in PROVIDERS:
        raise Invalid("unknown brain provider")
    effort = str(form.get("brain_effort", "low"))
    if effort not in EFFORTS:
        raise Invalid("unknown effort")
    model = str(form.get("brain_model", "")).strip()
    if not _MODEL_NAME.match(model):
        raise Invalid("the model name has characters it can't have")
    base_url = str(form.get("brain_base_url", "")).strip()
    if base_url and not re.match(r"^https?://[^\s]+$", base_url):
        raise Invalid("the base URL must start with http:// or https://")
    try:
        timeout = float(form.get("brain_timeout_s", ""))
    except ValueError:
        raise Invalid("the timeout must be a number of seconds") from None
    if not 1 <= timeout <= 3600:
        raise Invalid("the timeout must be between 1 and 3600 seconds")
    return dataclasses.replace(
        config,
        brain_provider=provider,
        brain_model=model,
        brain_base_url=base_url,
        brain_effort=effort,
        brain_web_search=form.get("brain_web_search") == "on",
        brain_timeout_s=timeout,
    )


def _restart_config(config: HostConfig, form) -> HostConfig:
    try:
        port = int(form.get("mcp_port", ""))
    except ValueError:
        raise Invalid("the MCP port must be a number") from None
    if not (port == 0 or 1024 <= port <= 65535) or port == config.port:
        raise Invalid("the MCP port must be 0 (off) or 1024 to 65535")
    speech_model = str(form.get("speech_model", "")).strip()
    if not _MODEL_NAME.match(speech_model):
        raise Invalid("the speech model name has characters it can't have")
    return dataclasses.replace(config, mcp_port=port, speech_model=speech_model)


def write_key(state: Path, name: str, value: str | None) -> None:
    """Set (or with None remove) one key in brain.env and in this process."""
    path = state / ENV_FILE
    lines = path.read_text().splitlines() if path.exists() else []
    kept = [line for line in lines if not line.startswith(f"{name}=")]
    if value is not None:
        kept.append(f"{name}={value}")
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(kept) + "\n")
    os.replace(tmp, path)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def _keys_present() -> dict[str, bool]:
    return {name: bool(os.environ.get(name)) for name in KEYS}


def routes(state: Path, parent) -> list[Route]:
    def render(request: Request, status_code: int = 200, **extra):
        config = parent.state.config
        return page(
            request,
            "settings.html",
            {
                "title": "Settings",
                "config": config,
                "on_disk": HostConfig.load(state / CONFIG_FILE),
                "providers": PROVIDERS,
                "efforts": EFFORTS,
                "keys": _keys_present(),
                "voices": voices(state),
                **extra,
            },
            status_code=status_code,
        )

    async def settings_page(request: Request):
        if request.method == "GET":
            return render(request, saved=request.query_params.get("saved"))
        if not await require_csrf(request):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        form = await request.form()
        section = form.get("section")
        config = parent.state.config
        try:
            if section == "brain":
                new = _brain_config(config, form)
                save_config(state, new)
                apply_config(parent, state, new)
                log.info("settings: brain set to %s", new.brain_provider or "off")
            elif section == "keys":
                cleared = form.get("clear")
                if cleared:
                    if cleared not in KEYS:
                        raise Invalid("unknown key")
                    write_key(state, str(cleared), None)
                    log.info("settings: %s removed", cleared)
                for name in KEYS:
                    value = str(form.get(name) or "").strip()
                    if not value:
                        continue
                    if not _SAFE_VALUE.match(value):
                        raise Invalid(f"{name} has characters a key can't have")
                    write_key(state, name, value)
                    log.info("settings: %s set", name)
                apply_config(parent, state, config)
            elif section == "voice":
                voice = str(form.get("tts_voice", ""))
                if voice and voice not in voices(state):
                    raise Invalid("that voice isn't downloaded")
                new = dataclasses.replace(config, tts_voice=voice)
                save_config(state, new)
                parent.state.config = new
                old = getattr(parent.state, "synthesizer", None)
                synthesizer = voice_out.Synthesizer(voice_out.engine_for(new, state / "models"))
                synthesizer.start()
                parent.state.synthesizer = synthesizer
                if old is not None:
                    old.close()
                log.info("settings: voice set to %s", voice or "off")
            elif section == "restart-settings":
                new = _restart_config(config, form)
                save_config(state, new)  # read at the next start
                log.info("settings: MCP port and speech model saved for the next start")
                return RedirectResponse("/dashboard/settings?saved=restart", status_code=303)
            elif section == "mcp-rotate":
                token = mcp_server.rotate_token(state)
                log.info("settings: MCP token rotated")
                return render(request, new_token=token)
            elif section == "restart":
                log.info("settings: restart requested from the dashboard")
                _exit_later(RESTART_EXIT)
                return render(request, restarting=True)
            else:
                raise Invalid("unknown settings section")
        except Invalid as exc:
            return render(request, status_code=400, error=str(exc))
        return RedirectResponse("/dashboard/settings?saved=1", status_code=303)

    return [Route("/settings", settings_page, methods=["GET", "POST"])]
