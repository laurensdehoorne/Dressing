#!/bin/sh
# Pulls the latest version from GitHub and rebuilds the container when something changed.
# Usage: ./update.sh          (or from cron: /opt/dressing/web/update.sh)
set -e
cd "$(dirname "$0")"
self="$(pwd)/$(basename "$0")"

# The pull can replace this script; the shell would then go on with the old version. So pull, then start the
# new copy of the script, which only compares (the commit from before the pull is passed along).
if [ -z "$DRESSING_BEFORE" ]; then
    DRESSING_BEFORE=$(git rev-parse HEAD)
    export DRESSING_BEFORE
    git pull --ff-only --quiet
    exec "$self" "$@"
fi
before=$DRESSING_BEFORE
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ] && [ "$1" != "--force" ]; then
    echo "$(date '+%F %T') al up-to-date ($(git log -1 --format=%h))"
    exit 0
fi

echo "$(date '+%F %T') bijwerken: $(git log -1 --format='%h %s')"
# Version shown in the app: the number of commits (goes up with every change), the date and the commit.
DRESSING_VERSION="$(git rev-list --count HEAD) ($(git log -1 --date=short --format='%cd, %h'))"
export DRESSING_VERSION
# The same setup as now runs: with or without the Tailscale container.
if docker ps --format '{{.Names}}' | grep -qx dressing-tailscale; then f=compose.tailscale.yaml; else f=compose.yaml; fi
docker compose -f "$f" up -d --build
docker image prune -f >/dev/null
