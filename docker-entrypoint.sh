#!/bin/sh
# Runtime uid/gid remapping, so a host can mount ./media without a root-owned
# directory appearing in it. The image is built with appuser=8888:8888; PUID and
# PGID move that user before the process starts.
#
# If the container was started with an explicit user (compose's `user:`, or
# `docker run -u`), there is nothing to remap and no privilege to do it with, so
# the command is exec'd straight away.
set -e

if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

PUID="${PUID:-8888}"
PGID="${PGID:-8888}"

current_gid="$(getent group appuser | cut -d: -f3)"
current_uid="$(id -u appuser)"

if [ "$current_gid" != "$PGID" ]; then
    groupmod -o -g "$PGID" appuser
fi

if [ "$current_uid" != "$PUID" ]; then
    usermod -o -u "$PUID" appuser
fi

if [ "$current_uid" != "$PUID" ] || [ "$current_gid" != "$PGID" ]; then
    # The whole tree, not just the volumes: usermod leaves every file the build
    # wrote (/app/.venv included) owned by the old id.
    chown -R "$PUID:$PGID" /app /home/appuser
else
    # A freshly created named volume is owned by root, whatever the id is.
    chown "$PUID:$PGID" /app/media /app/static
fi

exec gosu appuser "$@"
