#!/usr/bin/env bash
# For development: install this working tree (unpushed changes included) on a
# Pi you can ssh into, by running ../../install.sh there with MUSEHOST_SOURCE.
#
#   host/deploy/dev-deploy.sh [user@]host     (default: $MUSEHOST_PI or muse-host.local)
#
# Everyone else runs install.sh on the Pi itself; see the README.
set -euo pipefail

target=${1:-${MUSEHOST_PI:-muse-host.local}}
root=$(cd "$(dirname "$0")/../.." && pwd)
stage=/tmp/musehost-dev

# shellcheck disable=SC2029  # $stage is meant to expand here
ssh "$target" "rm -rf $stage && mkdir -p $stage/muse-gadget-sdk-selfhost"
rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
    --exclude state "$root/install.sh" "$root/host" "$target:$stage/"
rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache \
    "$root/muse-gadget-sdk-selfhost/linux" "$target:$stage/muse-gadget-sdk-selfhost/"
# shellcheck disable=SC2029
ssh -t "$target" "sudo MUSEHOST_SOURCE=$stage bash $stage/install.sh; rm -rf $stage"
