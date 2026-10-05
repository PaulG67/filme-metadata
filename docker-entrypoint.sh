#!/bin/sh
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"

if ! getent group "${PGID}" >/dev/null 2>&1; then
  groupadd -g "${PGID}" app || true
fi
if ! getent passwd "${PUID}" >/dev/null 2>&1; then
  useradd -u "${PUID}" -g "${PGID}" -M -s /usr/sbin/nologin app || true
fi

mkdir -p "${DATA_DIR:-/data}"
chown "${PUID}:${PGID}" "${DATA_DIR:-/data}" || true

exec gosu "${PUID}:${PGID}" \
  gunicorn "app.main:app" \
    --bind "0.0.0.0:${PORT:-8792}" \
    --workers 1 \
    --threads 8 \
    --timeout 180 \
    --access-logfile - \
    --error-logfile -
