#!/usr/bin/env bash
# Install or update musehost on a Raspberry Pi 5 (64-bit Raspberry Pi OS).
# Run it on the Pi itself, after logging in:
#
#   curl -fsSL https://raw.githubusercontent.com/jksim/muse-selfhost-server/main/install.sh | sudo bash
#
# It downloads this repo and the SDK's Linux client from GitHub, installs uv
# and the Python environment, creates the musehost user and /var/lib/musehost,
# runs `musehost init` the first time only (so the CA and devices survive
# updates), fetches the speech model and Clio's voice once, asks for an API key
# for Clio's brain on the first install, and (re)starts the service on port 443.
#
# Settings (environment variables, all optional):
#   MUSEHOST_HOSTNAME  name in the certificate (default: this Pi's <hostname>.local)
#   MUSEHOST_BIND      LAN address to serve on (default: the address of the default route)
#   MUSEHOST_REPO / MUSEHOST_REF        this repo on GitHub (jksim/muse-selfhost-server, main)
#   MUSEHOST_SDK_REPO / MUSEHOST_SDK_REF  the SDK (jksim/muse-gadget-sdk-selfhost, self-host)
#   MUSEHOST_SOURCE    a local checkout to install from instead of GitHub
#                      (holds host/ and muse-gadget-sdk-selfhost/linux/)
# Pass them through sudo: curl ... | sudo MUSEHOST_HOSTNAME=clio.local bash
set -euo pipefail

repo=${MUSEHOST_REPO:-jksim/muse-selfhost-server}
ref=${MUSEHOST_REF:-main}
sdk_repo=${MUSEHOST_SDK_REPO:-jksim/muse-gadget-sdk-selfhost}
sdk_ref=${MUSEHOST_SDK_REF:-self-host}
opt=/opt/musehost
state=/var/lib/musehost

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run it with sudo: curl -fsSL <url> | sudo bash"
[ "$(uname -m)" = aarch64 ] || die "needs 64-bit Raspberry Pi OS (this is $(uname -m))"
command -v systemctl >/dev/null || die "needs systemd"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
    || die "needs Python 3.11 or newer (Raspberry Pi OS Bookworm or later)"

hostname=${MUSEHOST_HOSTNAME:-$(hostname).local}
bind=${MUSEHOST_BIND:-$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' | head -1)}
[ -n "$bind" ] || die "no LAN address found; connect the Pi to your network or set MUSEHOST_BIND"
say "Installing musehost: https://$hostname on $bind:443"

missing=()
for tool in curl tar rsync; do command -v "$tool" >/dev/null || missing+=("$tool"); done
if [ ${#missing[@]} -gt 0 ]; then
    say "Installing ${missing[*]}"
    apt-get update -qq && apt-get install -y -qq "${missing[@]}"
fi

# The code: from GitHub as tarballs (no git needed), or a local checkout.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ -n "${MUSEHOST_SOURCE:-}" ]; then
    src=$(cd "$MUSEHOST_SOURCE" && pwd)
    sdk=$src/muse-gadget-sdk-selfhost
else
    say "Downloading $repo ($ref) and $sdk_repo ($sdk_ref)"
    mkdir -p "$work/src" "$work/sdk"
    curl -fsSL "https://github.com/$repo/archive/$ref.tar.gz" | tar -xz -C "$work/src" --strip-components=1
    curl -fsSL "https://github.com/$sdk_repo/archive/$sdk_ref.tar.gz" | tar -xz -C "$work/sdk" --strip-components=1
    src=$work/src
    sdk=$work/sdk
fi
[ -f "$src/host/pyproject.toml" ] || die "no host/ in $src"
[ -f "$sdk/linux/pyproject.toml" ] || die "no SDK linux/ in $sdk"

if ! command -v uv >/dev/null; then
    say "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

id musehost >/dev/null 2>&1 || useradd --system --home-dir "$state" --shell /usr/sbin/nologin musehost
getent group bluetooth >/dev/null && usermod -aG bluetooth musehost
install -d -m 0700 -o musehost -g musehost "$state"

say "Installing the code in $opt"
mkdir -p "$opt/muse-gadget-sdk-selfhost"
rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache "$src/host/" "$opt/host/"
rsync -a --delete --exclude .venv --exclude __pycache__ "$sdk/linux/" "$opt/muse-gadget-sdk-selfhost/linux/"
(cd "$opt/host" && UV_PYTHON_PREFERENCE=only-system uv sync --frozen --no-dev --quiet)

run() { sudo -u musehost env HF_HOME="$state/.cache/huggingface" "$opt/host/.venv/bin/musehost" --state-dir "$state" "$@"; }

if [ ! -f "$state/host.toml" ]; then
    say "Creating the host's CA, certificate and keys"
    run init --hostname "$hostname" --ip "$bind" --port 443
fi
# The CA certificate is public: firmware builds need it, so keep a readable copy.
install -m 0644 "$state/ca.pem" "$opt/ca.pem"

say "Fetching the speech model and Clio's voice (first time only)"
run download-model
run download-voice

# Clio's brain reads API keys from brain.env (0600). Ask once; never overwrite.
if [ ! -f "$state/brain.env" ]; then
    key=""
    # stdin is this script (curl | bash), so ask on the terminal, if there is one.
    if { : > /dev/tty; } 2>/dev/null; then
        printf '\nPaste an Anthropic API key for Clio (input hidden; Enter to skip): ' > /dev/tty
        IFS= read -rs key < /dev/tty || key=""
        printf '\n' > /dev/tty
    fi
    install -m 0600 -o musehost -g musehost /dev/null "$state/brain.env"
    {
        echo '# API keys for Clio (musehost brain). Uncomment the one host.toml brain_provider uses.'
        if [ -n "$key" ]; then printf 'ANTHROPIC_API_KEY=%s\n' "$key"; else echo '# ANTHROPIC_API_KEY='; fi
        echo '# OPENAI_API_KEY='
        echo '# VLLM_API_KEY='
    } > "$state/brain.env"
    if [ -n "$key" ]; then
        echo "Saved the key to $state/brain.env."
    else
        echo "No key saved; add one later with: sudoedit $state/brain.env"
    fi
    unset key
fi

cat > /usr/local/bin/musehost <<WRAP
#!/bin/sh
# Run musehost as its service account, against the service's state.
exec sudo -u musehost $opt/host/.venv/bin/musehost --state-dir $state "\$@"
WRAP
chmod 0755 /usr/local/bin/musehost

sed "s/@BIND@/$bind/" "$opt/host/deploy/musehost.service" > /etc/systemd/system/musehost.service
systemctl daemon-reload
systemctl enable --quiet musehost
systemctl restart musehost
sleep 3
systemctl is-active --quiet musehost || die "the service didn't start; see: journalctl -u musehost -n 50"

fingerprint=$(openssl x509 -noout -fingerprint -sha256 -in "$opt/ca.pem" 2>/dev/null | cut -d= -f2 || true)
cat <<DONE

musehost is running: https://$hostname (also https://$bind)
  Gadgets:    musehost devices list
  Pair one:   musehost pair --ssid "<your Wi-Fi>"
  Logs:       journalctl -u musehost -f
  CA for firmware builds: $opt/ca.pem${fingerprint:+ (SHA-256 $fingerprint)}

Run the same command again to update; your CA, devices and keys are kept.
DONE
