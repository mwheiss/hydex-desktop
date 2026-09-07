#!/bin/sh
set -eu

exec /usr/bin/python3 "${CODEX_LINUX_FEATURES_DIR:?}/remote-trust-race-workaround/manage.py" ensure \
  --app-dir "${CODEX_LINUX_APP_DIR:?}"
