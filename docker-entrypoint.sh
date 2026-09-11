#!/bin/sh
# Run the app as whichever user owns the mounted data directory.
#
# /data is a mount, so its ownership belongs to the host and not to this image.
# Unraid's /mnt/user/appdata is nobody:users (99:100), which a container fixed
# at uid 1000 cannot write -- the symptom is sqlite failing to open the database
# before anything else happens. So the account is movable: set PUID and PGID and
# the app runs as that user instead.
#
# Privileges are always dropped before the app starts. Root exists here only
# long enough to move the account and hand over the directory.
set -e

if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-1000}"
    PGID="${PGID:-1000}"

    # -o permits an id that already belongs to another account, which 99:100 does.
    groupmod -o -g "$PGID" tracker 2>/dev/null || true
    usermod -o -u "$PUID" -g "$PGID" tracker 2>/dev/null || true

    # One level deep only. If /data is ever pointed at a large share by mistake,
    # this must not recurse through it -- the app writes the database, its
    # journal files and routes.yml, all of them here.
    chown "$PUID:$PGID" /data 2>/dev/null || true
    find /data -maxdepth 1 -mindepth 1 -exec chown "$PUID:$PGID" {} + 2>/dev/null || true

    exec setpriv --reuid "$PUID" --regid "$PGID" --init-groups "$@"
fi

# Already non-root: compose was given a `user:`, so there is nothing to drop and
# nothing we are allowed to chown. If /data is wrong, the app now says so.
exec "$@"
