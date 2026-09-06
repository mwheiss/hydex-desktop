#!/bin/bash
set -euo pipefail
exec /usr/bin/python3 "${CODEX_LINUX_FEATURES_DIR:?}/persistent-app-server/manage.py" ensure-env
