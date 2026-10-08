# musehost: a self-hosted Muse

Your own host for Muse gadgets, instead of Meta's. A Raspberry Pi 5 runs
`musehost`; gadgets built from this repo's ESP32 firmware (branch `self-host`),
such as an M5Stack CoreS3, pair with it over Bluetooth and talk to it over the
LAN. It serves:
- the device API: enrollment, token refresh, the VM list;
- the Noise link: commands such as `device.health`;
- chat with Clio, the host's assistant:
  - she hears push-to-talk notes (Whisper, on the Pi);
  - she answers with Claude, OpenAI, a local vLLM, or a Hermes Agent on the Pi;
  - she can run a few gadget commands;
  - she speaks her replies through the gadget's speaker (Piper, on the Pi).

Everything is LAN-only, with TLS from the host's own CA and a pinned Noise key.

## Setting up the Raspberry Pi 5

No Linux machine is needed; everything below works from Windows or macOS.

You need:
- a Raspberry Pi 5 (4 GB or more) with power supply and a 16 GB+ microSD card;
- a network connection with internet access during the install;
- for Clio's brain, an [Anthropic API key](https://console.anthropic.com/)
  (or an OpenAI key, a local vLLM server, or a Hermes Agent on the Pi; see
  [Clio's brain](#clios-brain)).

1. **Write the OS.** In [Raspberry Pi Imager](https://www.raspberrypi.com/software/),
   pick *Raspberry Pi 5* and *Raspberry Pi OS (64-bit)* (Lite is fine). In the
   settings, set:
   - a hostname such as `muse-host`; gadgets reach the Pi as `<hostname>.local`;
   - your username and password;
   - Wi-Fi, unless the Pi is on Ethernet;
   - SSH on, if you'd rather log in over the network than plug in a screen.
2. **Log in to the Pi**, either at its own screen and keyboard or from your
   computer: `ssh <username>@muse-host.local` works in Windows PowerShell and
   the macOS Terminal.
3. **Install musehost:**

   ```sh
   curl -fsSL https://raw.githubusercontent.com/jksim/muse-gadget-sdk-selfhost/self-host/server/install.sh | sudo bash
   ```

   It takes a few minutes. It downloads the code, the speech model and Clio's
   voice (about 600 MB in all), asks for your API key and a
   [web dashboard](#web-dashboard) password (Enter skips either), and starts
   the service. Run the same command again later to update; the CA,
   paired gadgets and keys are kept.
4. **Give the Pi a fixed address**: a DHCP reservation in your router. The
   service serves its LAN address and the certificate names it.

Check it from the Pi with `musehost devices list` (empty at first) and
`journalctl -u musehost -f`.

## Setting up a gadget

Supported today: the **M5Stack CoreS3**. Everything happens on the Pi.

1. **Plug the gadget into one of the Pi's USB ports** with a data cable.
2. **Flash the firmware:**

   ```sh
   musehost flash
   ```

   It downloads the latest self-host firmware release, checks it, finds the
   gadget, says what it will write and asks first. It takes about a minute.
   The firmware is the same for every host: nothing about your Pi is built in.
3. **Pair it:**

   ```sh
   musehost pair --ssid "<your Wi-Fi>"
   ```

   Press the gadget's **power** button when asked. Pairing hands the gadget
   your Wi-Fi, its tokens, and this host's address, CA certificate and Noise
   key, so it trusts this Pi and no impostor.

To update the firmware later, run `musehost flash` again; Wi-Fi and pairing
are kept. `musehost flash --erase-settings` also clears them, for a fresh
start.

Building the firmware yourself is under [Development](#development).

## What install.sh does
- checks for 64-bit Raspberry Pi OS with Python 3.11+, and installs curl,
  tar or rsync if missing;
- adds the `musehost` user to `bluetooth` (pairing), and to `dialout` and
  `plugdev` (flashing: Raspberry Pi OS gives Espressif USB serial ports to
  `plugdev`);
- downloads this repo from GitHub as a tarball (no git needed) and installs
  `server/` and the SDK's `linux/` client it uses into `/opt/musehost`;
- installs uv and the Python environment;
- creates the `musehost` user with its state in `/var/lib/musehost` (0700);
- on the first install only:
  - runs `musehost init --hostname <hostname>.local --ip <LAN address> --port 443`;
  - asks for an Anthropic API key and saves it to `brain.env` (0600);
  - asks for a web dashboard password (hidden, twice, at least 12
    characters); Enter, three bad tries or no terminal leave the dashboard off;
- downloads the speech model and Clio's voice once (the service never
  downloads at run time);
- puts a readable copy of the CA certificate in `/opt/musehost/ca.pem`, for
  checking the host with `curl --cacert`;
- starts `musehost.service`, which binds 443 without root. Its sandbox allows
  no devices except USB serial (`ttyACM`), for flashing from the dashboard.

Optional settings, passed through sudo
(`curl … | sudo MUSEHOST_HOSTNAME=clio.local bash`):

| Variable | Default |
|---|---|
| `MUSEHOST_HOSTNAME` | the Pi's `<hostname>.local` (the certificate's name; first install only) |
| `MUSEHOST_BIND` | the Pi's LAN address |
| `MUSEHOST_REPO`, `MUSEHOST_REF` | `jksim/muse-gadget-sdk-selfhost`, `self-host` |
| `MUSEHOST_SOURCE` | install from a local checkout (holding `server/` and `linux/`) instead of GitHub |

On the Pi, `musehost …` (in `/usr/local/bin`) runs commands as the service
account, for example `musehost devices list`. Logs: `journalctl -u musehost -f`.

Give the Pi a DHCP reservation, because the service binds its LAN address and
the certificate names it. mDNS (`<hostname>.local`) over Wi-Fi can miss the
odd lookup; the IP always works.

## Gadget firmware and pairing

With the gadget (an M5Stack CoreS3) on one of the Pi's USB ports:

```sh
musehost flash                          # the newest self-host firmware release
musehost pair --ssid "<your Wi-Fi>"     # asks for the Wi-Fi password
```

Press the gadget's **power** button (not reset) when asked. `musehost devices
list` then shows it, and `musehost devices revoke NODE_ID` sends it back to
pairing.

`musehost flash`:
- downloads the newest `selfhost-v*` release of `firmware_repo` (`host.toml`,
  default `jksim/muse-gadget-sdk-selfhost`) into `/var/lib/musehost/firmware/`;
  `--version 0.1.0` picks one, `--file FW.zip` uses a local zip;
- checks every image against the release's manifest (SHA-256, board, and that
  nothing touches the settings or factory data);
- finds the gadget on USB (Espressif's vendor ID; `--port` when there are
  several) and lets esptool confirm the chip;
- says what it will write and asks (`--yes` skips the question), then writes
  each image at its own offset and restarts the gadget.

The gadget's settings (Wi-Fi and pairing, in its NVS partition) are kept, so
an update needs no re-pairing. `--erase-settings` clears them too. Use it once
for a gadget that was paired with older firmware that had the CA built in,
and whenever you want a clean start.

The firmware is the same for every host. Pairing gives it this host's address,
CA certificate and Noise key: it trusts that CA only for this host (other
sites keep the public roots) and refuses a host presenting a different Noise
key.

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
| `musehost flash [--board B] [--port P] [--version V \| --file F] [--erase-settings] [--yes]` | Write the self-host firmware to a gadget on USB |
| `musehost dashboard-password [--stdin \| --off]` | Set the [web dashboard](#web-dashboard) password, which turns it on, and sign out its sessions; `--off` turns it off |
| `musehost mcp-config [--rotate]` | Print the block that lets Hermes Agent (or another MCP client) use the gadgets; `--rotate` replaces the token |

`--state-dir DIR` (or `$MUSEHOST_STATE_DIR`) picks another state directory.
The service reads `host.toml` from the state directory.

## Web dashboard

`https://<hostname>.local/dashboard`, served by musehost itself on 443 with
the host's own certificate. It is off (every page a 404) until it has a
password: the one chosen during the install, or set later on the Pi with

```sh
musehost dashboard-password        # asks twice; at least 12 characters
musehost dashboard-password --off  # turns it off again
```

Setting or changing the password signs out every browser. There is one
password and no username.

**Trusting the CA.** Browsers warn about the dashboard until they trust the
host's CA. Open `https://<hostname>.local/dashboard/ca` (past the warning,
once), check that the fingerprint matches the one
`musehost dashboard-password` printed, download the certificate and follow
the steps for Windows, macOS or Linux shown there. The CA is this Pi's own;
trusting it lets the browser accept this host only.

**Pages:**
- **Status:** version and uptime, the CA fingerprint, the speech model, voice,
  brain and MCP states, gadget counts and musehost's recent log lines.
- **Gadgets:** every paired gadget, online or not, updating live. A
  gadget's page checks its health (battery, memory, Wi-Fi and so on), lists
  its commands and which of them Clio may use, and revokes it after you type
  its name (an online gadget is told, and goes back to pairing).
- **Pair:** scans for gadgets in setup mode, then pairs the one you choose
  with the Wi-Fi network you give (the password is used once and not
  stored). The steps show live, including when to press the gadget's button;
  the page can be closed and reopened while it runs.
- **Flash:** writes the newest self-host release (or a version you name) to
  the gadget plugged into the Pi's USB, showing esptool's progress. The
  download is checked before anything is written; erasing the gadget's
  settings needs the word `erase` typed.
- **Settings:**
  - the brain (provider, model, base URL, effort, web search, timeout),
    applied to the next turn;
  - API keys, write-only: the page shows only whether each is set; they're
    saved to `brain.env`;
  - the voice, from the Piper voices already downloaded;
  - rotating the MCP token (the new one is shown once);
  - the MCP port and speech model, which apply after a restart, and a
    restart button.

**Security:**
- sign-ins are limited to 5 wrong passwords a minute per address, then 15
  minutes of refusal;
- sessions end after 30 days idle;
- every change needs the page's CSRF token;
- pages carry a strict Content-Security-Policy and load nothing from the
  internet;
- the password is stored only as a salted scrypt hash in
  `/var/lib/musehost/dashboard.pw`.

## Clio's brain

Clio answers with a language model chosen in `host.toml`:

| `brain_provider` | Needs | Notes |
|---|---|---|
| `claude` (default) | `ANTHROPIC_API_KEY` | `brain_model` defaults to `claude-opus-5-5` at `brain_effort = "low"`; web search when `brain_web_search = true` |
| `openai` | `OPENAI_API_KEY`, `brain_model` | Chat Completions with function tools; no web search |
| `vllm` | `brain_base_url`, `brain_model` (key optional: `VLLM_API_KEY`) | Same adapter as OpenAI; start vLLM with `--enable-auto-tool-choice --tool-call-parser <parser for your model>` so tools work |
| `hermes` | `HERMES_API_KEY` | A [Hermes Agent](https://github.com/NousResearch/hermes-agent) on the Pi answers as Clio; `brain_base_url` defaults to `http://127.0.0.1:8642/v1`, `brain_model` to `hermes-agent`. See below |
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
- `brain_timeout_s` (default 120) caps a turn; past it, Clio says she
  couldn't reach her brain.

### Hermes Agent

[Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research)
can be Clio's brain. It brings its own memory, skills, scheduled tasks and
messaging channels, and it uses the gadgets through musehost's MCP server
(below). musehost doesn't install or run Hermes. Expect slower answers: Clio's
first words took about 7–16 s through Hermes on a Pi 5, against about
1.5–4 s with Claude directly. On the Pi, as your login:

1. **Install Hermes** with its installer (see Hermes's own docs). It clones a
   large repository, so it can look frozen for 10–15 minutes; let it finish.
   A failed web-UI build at the end doesn't matter here. The `hermes` command
   lands in `~/.local/bin`.
2. **Pick a model:** `hermes setup` (an Anthropic key works). Check that
   `hermes` answers you.
3. **Turn on its API server:** in `~/.hermes/.env`, add
   `API_SERVER_ENABLED=true` and `API_SERVER_KEY=<a long random string>`, for
   example from `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
4. **Let it use the gadgets:** run `musehost mcp-config` and add the printed
   block to `~/.hermes/config.yaml`.
5. **Keep it running:** `hermes setup` may already have installed its gateway
   as a user service; if not, `hermes gateway install`. After the changes
   above, restart it with `systemctl --user restart hermes-gateway`. Check that
   `hermes gateway status` says it's running and that lingering is on
   (otherwise `sudo loginctl enable-linger $USER`), that `hermes mcp list`
   shows `musehost` enabled, and that the API server answers on
   127.0.0.1:8642.
6. **Point musehost at it:** add `HERMES_API_KEY=<the same string>` to
   `/var/lib/musehost/brain.env` (keep it 0600, owned by `musehost`), set
   `brain_provider = "hermes"` in `/var/lib/musehost/host.toml`, then
   `sudo systemctl restart musehost`.

Clio keeps her name and her short, speakable replies: musehost sends her
instructions with every turn, including which gadget she's talking to so
Hermes can pass it to the MCP tools. Each musehost conversation is one Hermes
conversation, which Hermes keeps. The journal shows `brain: hermes at
http://127.0.0.1:8642/v1`; if Hermes is down, Clio says she couldn't reach
her brain.

To go back to Claude, set `brain_provider = "claude"` and restart musehost.
Both keys stay in `brain.env`. To free Hermes's memory (about 330 MB) while
it's not in use: `systemctl --user disable --now hermes-gateway`. Use
`enable --now` to bring it back.

## Gadgets over MCP

musehost also serves the paired gadgets as **MCP tools** (Model Context
Protocol), so an agent running on the Pi, such as Hermes Agent, can use them.
For example, it can check a gadget's health or put a picture on its screen
from a Telegram chat.

- **Where:** `http://127.0.0.1:8765/mcp`, on the Pi only (`mcp_port` in
  `host.toml`; `0` turns it off). Requests need the token in
  `/var/lib/musehost/mcp.token`.
- **Tools:** `list_gadgets`, plus one tool per command in `brain_tools`
  (`device_health`, `display_draw_url`, `display_show_animation`), each taking
  a `gadget` node id; it can be left out when only one gadget is online.
  `device.ota` is never offered, and nothing can pair, unpair or revoke.
- **Logs:** arguments and results are never logged.

To connect a client, print its configuration block on the Pi:

```sh
musehost mcp-config            # paste into ~/.hermes/config.yaml
musehost mcp-config --rotate   # new token; the old one stops working at once
```

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
SDK's Linux client from `../linux` (editable).

```sh
cd server
uv sync
uv run pytest -q                      # fast suite
uv run pytest -q -m slow              # real Whisper and Piper models (downloads once)
uv run ruff check . && uv run ruff format --check .
```

Gadget firmware: `.github/workflows/selfhost-release.yml` builds and
publishes it for a `selfhost-v<version>` tag. To build and flash it from a
checkout instead (ESP-IDF v6.0.1 on macOS or Linux), from the repo root:

```sh
esp32/tools/muse/selfhost.sh build cores3
esp32/tools/muse/selfhost.sh flash cores3 /dev/ttyACM0
esp32/tools/muse/selfhost.sh log /dev/ttyACM0 60      # serial output, under logs/
```

`esp32/tools/muse/package_selfhost.py` turns such a
build into the same zip a release has, for `musehost flash --file`.

To try unpushed changes on a Pi you can ssh into, `server/deploy/dev-deploy.sh
[user@]host` copies this working tree there and runs `install.sh` with
`MUSEHOST_SOURCE`.

To run a host on the dev machine instead of the Pi, in `server/`:

```sh
uv run musehost init --hostname "$(hostname).local" --ip <lan-ip>   # https on :8443
uv run musehost serve --bind <lan-ip>
```

`init` writes `./state` (0700): the CA, the server cert, the Noise key and the
database. Devices are provisioned with the first `--hostname` and the port, so
pick them before pairing anything. Changing either later means pairing every
device again.
