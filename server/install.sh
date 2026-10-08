#!/usr/bin/env bash
# Install or update musehost on a Raspberry Pi 5 (64-bit Raspberry Pi OS).
# Run it on the Pi itself, after logging in:
#
#   curl -fsSL https://raw.githubusercontent.com/jksim/muse-gadget-sdk-selfhost/self-host/server/install.sh | sudo bash
#
# It downloads this repo (the server and the SDK's Linux client it uses) from
# GitHub, installs uv
# and the Python environment, creates the musehost user and /var/lib/musehost,
# runs `musehost init` the first time only (so the CA and devices survive
# updates), fetches the speech model and Clio's voice once, asks for an API key
# for Clio's brain and a web dashboard password on the first install, and
# (re)starts the service on port 443.
#
# Settings (environment variables, all optional):
#   MUSEHOST_HOSTNAME  name in the certificate (default: this Pi's <hostname>.local)
#   MUSEHOST_BIND      LAN address to serve on (default: the address of the default route)
#   MUSEHOST_REPO / MUSEHOST_REF  this repo on GitHub (jksim/muse-gadget-sdk-selfhost, self-host)
#   MUSEHOST_SOURCE    a local checkout to install from instead of GitHub
#                      (holds server/ and linux/)
# Pass them through sudo: curl ... | sudo MUSEHOST_HOSTNAME=clio.local bash
set -euo pipefail

repo=${MUSEHOST_REPO:-jksim/muse-gadget-sdk-selfhost}
ref=${MUSEHOST_REF:-self-host}
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

# The code: from GitHub as a tarball (no git needed), or a local checkout.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
if [ -n "${MUSEHOST_SOURCE:-}" ]; then
    src=$(cd "$MUSEHOST_SOURCE" && pwd)
else
    say "Downloading $repo ($ref)"
    mkdir -p "$work/src"
    curl -fsSL "https://github.com/$repo/archive/$ref.tar.gz" | tar -xz -C "$work/src" --strip-components=1
    src=$work/src
fi
[ -f "$src/server/pyproject.toml" ] || die "no server/ in $src"
[ -f "$src/linux/pyproject.toml" ] || die "no linux/ (the SDK's Linux client) in $src"

if ! command -v uv >/dev/null; then
    say "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

id musehost >/dev/null 2>&1 || useradd --system --home-dir "$state" --shell /usr/sbin/nologin musehost
getent group bluetooth >/dev/null && usermod -aG bluetooth musehost
# `musehost flash` writes firmware to a gadget on USB serial. USB serial ports
# are usually group dialout, but Raspberry Pi OS's OpenOCD udev rules give
# Espressif's USB-JTAG serial (the CoreS3's port) to plugdev.
for group in dialout plugdev; do
    getent group "$group" >/dev/null && usermod -aG "$group" musehost
done
install -d -m 0700 -o musehost -g musehost "$state"

say "Installing the code in $opt"
mkdir -p "$opt"
rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude state \
    "$src/server/" "$opt/server/"
rsync -a --delete --exclude .venv --exclude __pycache__ "$src/linux/" "$opt/linux/"
(cd "$opt/server" && UV_PYTHON_PREFERENCE=only-system uv sync --frozen --no-dev --quiet)

run() { sudo -u musehost env HF_HOME="$state/.cache/huggingface" "$opt/server/.venv/bin/musehost" --state-dir "$state" "$@"; }

first_install=0
if [ ! -f "$state/host.toml" ]; then
    first_install=1
    say "Creating the host's CA, certificate and keys"
    run init --hostname "$hostname" --ip "$bind" --port 443
fi
# The CA certificate is public; keep a readable copy for checking the host (curl --cacert).
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
        echo '# HERMES_API_KEY='
    } > "$state/brain.env"
    if [ -n "$key" ]; then
        echo "Saved the key to $state/brain.env."
    else
        echo "No key saved; add one later with: sudoedit $state/brain.env"
    fi
    unset key
fi

# BEGIN dashboard-password (tests/test_install_prompt.py runs this block)
# The web dashboard stays off until it has a password. Ask for one on the
# terminal; Enter, three bad tries or no terminal leave it off.
ask_dashboard_password() {
    local pw again
    if ! { : > /dev/tty; } 2>/dev/null; then
        echo "No terminal: the dashboard stays off. Turn it on with: musehost dashboard-password"
        return 0
    fi
    for _ in 1 2 3; do
        printf '\nChoose a web dashboard password (12+ characters, hidden; Enter to skip): ' > /dev/tty
        IFS= read -rs pw < /dev/tty || pw=""
        printf '\n' > /dev/tty
        if [ -z "$pw" ]; then
            echo "Skipped: the dashboard stays off. Turn it on with: musehost dashboard-password"
            return 0
        fi
        if [ "${#pw}" -lt 12 ]; then
            echo "It needs at least 12 characters." > /dev/tty
            continue
        fi
        printf 'Again: ' > /dev/tty
        IFS= read -rs again < /dev/tty || again=""
        printf '\n' > /dev/tty
        if [ "$pw" != "$again" ]; then
            echo "They don't match." > /dev/tty
            continue
        fi
        if printf '%s\n' "$pw" | run dashboard-password --stdin > /dev/null; then
            dashboard_on=1
            return 0
        fi
    done
    echo "No password set: the dashboard stays off. Turn it on with: musehost dashboard-password"
}
# END dashboard-password

dashboard_on=0
if [ "$first_install" = 1 ]; then
    ask_dashboard_password
fi
[ -f "$state/dashboard.pw" ] && dashboard_on=1

cat > /usr/local/bin/musehost <<WRAP
#!/bin/sh
# Run musehost as its service account, against the service's state.
exec sudo -u musehost $opt/server/.venv/bin/musehost --state-dir $state "\$@"
WRAP
chmod 0755 /usr/local/bin/musehost

# systemd refuses a unit naming a group that doesn't exist; keep the ones that do.
groups=""
for group in dialout plugdev bluetooth; do
    getent group "$group" >/dev/null && groups="$groups $group"
done
sed -e "s/@BIND@/$bind/" -e "s/^SupplementaryGroups=.*/SupplementaryGroups=${groups# }/" \
    "$opt/server/deploy/musehost.service" > /etc/systemd/system/musehost.service
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
  Gadget:     plug it into USB, then: musehost flash
  CA:         $opt/ca.pem${fingerprint:+ (SHA-256 $fingerprint)}
DONE
if [ "$dashboard_on" = 1 ]; then
    cat <<DONE
  Dashboard:  https://$hostname/dashboard (first trust the CA: https://$hostname/dashboard/ca)
DONE
else
    cat <<DONE
  Dashboard:  off; turn it on with: musehost dashboard-password
DONE
fi
cat <<DONE

Run the same command again to update; your CA, devices and keys are kept.
DONE
