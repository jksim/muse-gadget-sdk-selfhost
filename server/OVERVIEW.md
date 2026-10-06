# muse-selfhost-server

Your own host for Muse gadgets, instead of Meta's. A Raspberry Pi 5 runs the
host (`musehost`). Gadgets built from
[muse-gadget-sdk-selfhost](https://github.com/jksim/muse-gadget-sdk-selfhost) (branch
`self-host`), such as an M5Stack CoreS3, pair with it over Bluetooth and talk
to it over the LAN.

Clio, the host's assistant:
- hears push-to-talk notes (Whisper, on the Pi);
- answers with Claude, OpenAI or a local vLLM;
- can run a few gadget commands;
- speaks her replies through the gadget's speaker (Piper, on the Pi).

Everything is LAN-only, with TLS from the host's own CA and a pinned Noise key.

## Layout

```
host/               the musehost Python package, tests and Pi installer (see host/README.md)
esp32/build.sh      fetch the host CA, build, flash and log the gadget firmware
muse-gadget-sdk-selfhost/    for development: the SDK checkout, branch self-host (not tracked here)
```

## Setting up the Raspberry Pi 5

No Linux machine is needed; everything below works from Windows or macOS.

You need:
- a Raspberry Pi 5 (4 GB or more) with power supply and a 16 GB+ microSD card;
- a network connection with internet access during the install;
- for Clio's brain, an [Anthropic API key](https://console.anthropic.com/)
  (or an OpenAI key or a local vLLM server; see `host/README.md`).

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
   curl -fsSL https://raw.githubusercontent.com/jksim/muse-selfhost-server/main/install.sh | sudo bash
   ```

   It takes a few minutes. It downloads the code, the speech model and Clio's
   voice (about 600 MB in all), asks for your API key (Enter skips it), and
   starts the service. Run the same command again later to update; the CA,
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

Building the firmware yourself is in `host/README.md` (Development).

## Tests

```sh
cd host && uv run pytest -q && uv run ruff check . && uv run ruff format --check .
```
