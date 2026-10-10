#!/bin/sh
set -eu

# A bind mount hides the CA generated while the image is built. Seed a fresh
# DSM data directory on first start, while preserving all later state.
if [ ! -d /app/ots/ca ] || [ -z "$(find /app/ots/ca -mindepth 1 -maxdepth 1 -type f -print -quit)" ]; then
  cp -a /app/ots-seed/. /app/ots/
fi

chown -R ots:ots /app/ots
exec gosu ots opentakserver "$@"
