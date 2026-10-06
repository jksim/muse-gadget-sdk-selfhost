"""Host settings, stored as ``host.toml`` in the state directory.

Devices are provisioned with ``public_host`` (the first hostname, else the
first IP, plus the port), so changing either after enrollment strands them.
"""

from __future__ import annotations

import ipaddress
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8443
DEFAULT_VM_ID = "home"
DEFAULT_AGENT_NAME = "Clio"
DEFAULT_SPEECH_MODEL = "base.en"
DEFAULT_SPEECH_TIMEOUT_S = 30.0
DEFAULT_TTS_VOICE = "en_US-lessac-medium"
DEFAULT_FIRMWARE_REPO = "jksim/muse-gadget-sdk-selfhost"
DEFAULT_MCP_PORT = 8765
DEFAULT_BRAIN_TOOLS = ("device.health", "display.draw_url", "display.show_animation")
STATE_DIR_ENV = "MUSEHOST_STATE_DIR"
DEFAULT_STATE_DIR = Path("state")
CONFIG_FILE = "host.toml"


def state_dir(override: str | None = None) -> Path:
    return Path(override or os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR)


@dataclass(frozen=True)
class HostConfig:
    hostnames: tuple[str, ...]
    ips: tuple[str, ...]
    port: int = DEFAULT_PORT
    vm_id: str = DEFAULT_VM_ID
    agent_name: str = DEFAULT_AGENT_NAME
    speech_model: str = DEFAULT_SPEECH_MODEL  # "" turns speech off
    speech_timeout_s: float = DEFAULT_SPEECH_TIMEOUT_S
    # brain: who answers as Clio. "" turns it off (the placeholder answers).
    brain_provider: str = "claude"  # claude | openai | vllm | ""
    brain_model: str = ""  # "" = the provider's default (claude: claude-opus-5-5)
    brain_base_url: str = ""  # vllm (or an OpenAI-compatible endpoint)
    brain_effort: str = "low"  # claude only
    brain_web_search: bool = True  # claude only
    brain_tools: tuple[str, ...] = DEFAULT_BRAIN_TOOLS  # commands Clio may run
    brain_idle_minutes: int = 30
    brain_max_tokens: int = 4096
    brain_timeout_s: float = 120.0  # longest a turn may take (Hermes tool runs can be long)
    tts_voice: str = DEFAULT_TTS_VOICE  # Piper voice for spoken replies; "" turns speech off
    firmware_repo: str = DEFAULT_FIRMWARE_REPO  # GitHub repo whose releases `musehost flash` uses
    mcp_port: int = DEFAULT_MCP_PORT  # gadgets as MCP tools on 127.0.0.1; 0 turns it off

    @property
    def address(self) -> str:
        if self.hostnames:
            return self.hostnames[0]
        ip = ipaddress.ip_address(self.ips[0])
        return f"[{ip}]" if ip.version == 6 else str(ip)

    @property
    def public_host(self) -> str:
        """``noise_host`` as provisioned to devices.

        On 443 it is the bare address: the ESP32 firmware always dials 443 and
        uses ``noise_host`` verbatim as a hostname. Other ports need
        ``host:port``, which only the Linux client understands.
        """
        if self.port == 443:
            return self.address
        return f"{self.address}:{self.port}"

    @property
    def api_url(self) -> str:
        """Provisioned to devices as ``api_url_v2``."""
        return f"https://{self.public_host}"

    def save(self, path: Path) -> None:
        # JSON strings and integers are valid TOML values for these keys.
        lines = [
            f"hostnames = {json.dumps(list(self.hostnames))}",
            f"ips = {json.dumps(list(self.ips))}",
            f"port = {self.port}",
            f"vm_id = {json.dumps(self.vm_id)}",
            f"agent_name = {json.dumps(self.agent_name)}",
            f"speech_model = {json.dumps(self.speech_model)}",
            f"speech_timeout_s = {float(self.speech_timeout_s)}",
            f"brain_provider = {json.dumps(self.brain_provider)}",
            f"brain_model = {json.dumps(self.brain_model)}",
            f"brain_base_url = {json.dumps(self.brain_base_url)}",
            f"brain_effort = {json.dumps(self.brain_effort)}",
            f"brain_web_search = {'true' if self.brain_web_search else 'false'}",
            f"brain_tools = {json.dumps(list(self.brain_tools))}",
            f"brain_idle_minutes = {int(self.brain_idle_minutes)}",
            f"brain_max_tokens = {int(self.brain_max_tokens)}",
            f"brain_timeout_s = {float(self.brain_timeout_s)}",
            f"tts_voice = {json.dumps(self.tts_voice)}",
            f"firmware_repo = {json.dumps(self.firmware_repo)}",
            f"mcp_port = {int(self.mcp_port)}",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> HostConfig:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return cls(
            hostnames=tuple(data.get("hostnames", ())),
            ips=tuple(data.get("ips", ())),
            port=int(data.get("port", DEFAULT_PORT)),
            vm_id=data.get("vm_id", DEFAULT_VM_ID),
            agent_name=data.get("agent_name", DEFAULT_AGENT_NAME),
            speech_model=data.get("speech_model", DEFAULT_SPEECH_MODEL),
            speech_timeout_s=float(data.get("speech_timeout_s", DEFAULT_SPEECH_TIMEOUT_S)),
            brain_provider=data.get("brain_provider", "claude"),
            brain_model=data.get("brain_model", ""),
            brain_base_url=data.get("brain_base_url", ""),
            brain_effort=data.get("brain_effort", "low"),
            brain_web_search=bool(data.get("brain_web_search", True)),
            brain_tools=tuple(data.get("brain_tools", DEFAULT_BRAIN_TOOLS)),
            brain_idle_minutes=int(data.get("brain_idle_minutes", 30)),
            brain_max_tokens=int(data.get("brain_max_tokens", 4096)),
            brain_timeout_s=float(data.get("brain_timeout_s", 120.0)),
            tts_voice=data.get("tts_voice", DEFAULT_TTS_VOICE),
            firmware_repo=data.get("firmware_repo", DEFAULT_FIRMWARE_REPO),
            mcp_port=int(data.get("mcp_port", DEFAULT_MCP_PORT)),
        )
