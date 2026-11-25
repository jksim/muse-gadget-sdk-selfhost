#!/bin/sh
# Keep this Pi's mDNS name. Avahi 0.8 can see a false name conflict at boot
# and rename the Pi (muse-host-2.local); gadgets then can't find the host.
# Run by musehost-mdns.timer as root: musehost-mdns.sh <the host's name>
set -eu
want=$1
# Only a .local name for this Pi is Avahi's to publish.
[ "$want" = "$(hostname -s).local" ] || exit 0
have=$(busctl call org.freedesktop.Avahi / org.freedesktop.Avahi.Server GetHostNameFqdn \
    | sed -n 's/^s "\(.*\)"$/\1/p')
[ "$have" = "$want" ] && exit 0
systemctl restart avahi-daemon
echo "Avahi was $have, not $want; restarted it"
