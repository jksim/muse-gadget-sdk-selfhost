# musehost

A self-hosted Muse host. Gadgets from `muse-gadget-sdk-selfhost` (branch `self-host`)
pair with it and connect to it instead of Muse's servers. It serves:
- the device API: enrollment, token refresh, the VM list;
- the Noise link: commands such as `device.health`;
- chat with Clio: speech to text (Whisper), a language model (Claude, OpenAI
  or vLLM), and spoken replies (Piper).

## Installing on a Raspberry Pi

On the Pi, after logging in (see the top-level README for preparing it):

```sh
curl -fsSL https://raw.githubusercontent.com/jksim/muse-selfhost-server/main/install.sh | sudo bash
```

`install.sh`:
- checks for 64-bit Raspberry Pi OS with Python 3.11+, and installs curl,
  tar or rsync if missing;
- downloads this repo and the SDK's `linux/` client from GitHub as tarballs
  (no git needed) into `/opt/musehost`;
- installs uv and the Python environment;
- creates the `musehost` user with its state in `/var/lib/musehost` (0700);
- on the first install only:
  - runs `musehost init --hostname <hostname>.local --ip <LAN address> --port 443`;
  - asks for an Anthropic API key and saves it to `brain.env` (0600);
- downloads the speech model and Clio's voice once (the service never
  downloads at run time);
- puts a readable copy of the CA certificate in `/opt/musehost/ca.pem`, for
  firmware builds;
- starts `musehost.service`, which binds 443 without root.

Optional settings, passed through sudo
(`curl … | sudo MUSEHOST_HOSTNAME=clio.local bash`):

| Variable | Default |
|---|---|
| `MUSEHOST_HOSTNAME` | the Pi's `<hostname>.local` (the certificate's name; first install only) |
| `MUSEHOST_BIND` | the Pi's LAN address |
| `MUSEHOST_REPO`, `MUSEHOST_REF` | `jksim/muse-selfhost-server`, `main` |
| `MUSEHOST_SDK_REPO`, `MUSEHOST_SDK_REF` | `jksim/muse-gadget-sdk-selfhost`, `self-host` |
| `MUSEHOST_SOURCE` | install from a local checkout instead of GitHub |

On the Pi, `musehost …` (in `/usr/local/bin`) runs commands as the service
account, for example `musehost devices list`. Logs: `journalctl -u musehost -f`.

Give the Pi a DHCP reservation, because the service binds its LAN address and
the certificate names it. mDNS (`<hostname>.local`) over Wi-Fi can miss the
odd lookup; the IP always works.

## Pairing a gadget

Build and flash the firmware with the host's CA (`esp32/build.sh`, see
`muse-gadget-sdk-selfhost/esp32/AGENTS.md`, "A self-hosted Muse"). Then pair from the
Pi over Bluetooth:

```sh
musehost pair --ssid "<your Wi-Fi>"    # on the Pi; asks for the Wi-Fi password
```

Press the gadget's **power** button (not reset) when asked. `musehost devices
list` then shows it, and `musehost devices revoke NODE_ID` sends it back to
pairing.

## Commands

| Command | What it does |
|---|---|
| `musehost init --hostname H [--ip IP] [--port N] [--force]` | Create host state (port 8443 unless given); refuses to overwrite without `--force` |
| `musehost serve [--port N] [--bind ADDR]` | Serve the device API, Noise link and chat over TLS |
| `musehost pair [--ssid S] [--name N] [--scan-only]` | Pair and provision a gadget over Bluetooth |
| `musehost enroll NODE_ID --out FILE [--display-name NAME]` | Enroll a gadget by hand; writes its `pairing.json` (0600) |
| `musehost grant` | One-time code, host URL and CA fingerprint for a pairing app |
| `musehost devices list` / `revoke NODE_ID` | Enrolled gadgets; cut one off |
| `musehost invoke NODE_ID COMMAND [JSON]` | Run a command on an online gadget, e.g. `device.health` |
| `musehost chat [--device NODE_ID] [--new]` | Talk to Clio from a terminal |
| `musehost download-model` / `transcribe FILE.wav` | Fetch the Whisper model once; transcribe a WAV |
| `musehost download-voice [NAME]` / `say TEXT [--out F.mp3]` | Fetch the Piper voice once; speak text into an MP3 with timings |

`--state-dir DIR` (or `$MUSEHOST_STATE_DIR`) picks another state directory.
The service reads `host.toml` from the state directory.

## Clio's brain

Clio answers with a language model chosen in `host.toml`:

| `brain_provider` | Needs | Notes |
|---|---|---|
| `claude` (default) | `ANTHROPIC_API_KEY` | `brain_model` defaults to `claude-opus-5-5` at `brain_effort = "low"`; web search when `brain_web_search = true` |
| `openai` | `OPENAI_API_KEY`, `brain_model` | Chat Completions with function tools; no web search |
| `vllm` | `brain_base_url`, `brain_model` (key optional: `VLLM_API_KEY`) | Same adapter as OpenAI; start vLLM with `--enable-auto-tool-choice --tool-call-parser <parser for your model>` so tools work |
| `""` | — | Brain off: a placeholder answers |

Keys live in `/var/lib/musehost/brain.env` (0600, owned by `musehost`), one
`NAME=value` per line. The installer asks for an Anthropic key the first
time. To add or change keys later, on the Pi:

```sh
sudoedit /var/lib/musehost/brain.env     # ANTHROPIC_API_KEY=...
sudo systemctl restart musehost
```

The journal says which provider started (`brain: claude with
claude-opus-5-5`) or what is missing.

Tools and conversations:
- Clio may run only the commands in `brain_tools` (default `device.health`,
  `display.draw_url`, `display.show_animation`). `device.ota` is never
  offered.
- Conversations are kept per gadget for 30 idle minutes
  (`brain_idle_minutes`), then start fresh.
- With Claude or OpenAI, the conversation leaves the LAN.

## Speech

- **Listening:** voice notes are transcribed on the Pi with faster-whisper
  (`speech_model`, default `base.en`), about 2 s per note.
- **Speaking:** gadgets built with `CONFIG_MUSE_TTS_PATH="/tts"` post each reply
  to `POST /tts` on the chat session and play it while their speaker setting is
  on. The reply is synthesised by Piper (`tts_voice`, default
  `en_US-lessac-medium`) and streamed sentence by sentence as 48 kbps CBR mono
  MP3; the first sentence is ready in about 0.4 s on a Pi 5.
- **Changing or stopping the voice:** `tts_voice = ""` turns speech off and
  gadgets fall back to captions. For another voice, run
  `musehost download-voice NAME` then restart the service.

Neither transcripts nor reply text are logged.

## Development

Needs [uv](https://docs.astral.sh/uv/) and Python 3.11+. The tests import the
SDK from `../muse-gadget-sdk-selfhost/linux` (editable), so check out its `self-host`
branch first.

```sh
cd host
uv sync
uv run pytest -q                      # fast suite
uv run pytest -q -m slow              # real Whisper and Piper models (downloads once)
uv run ruff check . && uv run ruff format --check .
```

To try unpushed changes on a Pi you can ssh into, `host/deploy/dev-deploy.sh
[user@]host` copies this working tree there and runs `install.sh` with
`MUSEHOST_SOURCE`.

To run a host on the dev machine instead of the Pi:

```sh
uv run musehost init --hostname "$(hostname).local" --ip <lan-ip>   # https on :8443
uv run musehost serve --bind <lan-ip>
```

`init` writes `./state` (0700): the CA, the server cert, the Noise key and the
database. Devices are provisioned with the first `--hostname` and the port, so
pick them before pairing anything. Changing either later means pairing every
device again.
