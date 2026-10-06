#!/usr/bin/env bash
# For development: install this working tree (unpushed changes included) on a
# Pi you can ssh into, by running server/install.sh there with MUSEHOST_SOURCE.
#
#   server/deploy/dev-deploy.sh [user@]host     (default: $MUSEHOST_PI or muse-host.local)
#
# Everyone else runs install.sh on the Pi itself; see server/README.md.
set -euo pipefail

target=${1:-${MUSEHOST_PI:-muse-host.local}}
root=$(cd "$(dirname "$0")/../.." && pwd)
stage=/tmp/musehost-dev

# shellcheck disable=SC2029  # $stage is meant to expand here
ssh "$target" "rm -rf $stage && mkdir -p $stage"
rsync -a --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
    --exclude state "$root/server" "$root/linux" "$target:$stage/"
# shellcheck disable=SC2029
ssh -t "$target" "sudo MUSEHOST_SOURCE=$stage bash $stage/server/install.sh; rm -rf $stage"
