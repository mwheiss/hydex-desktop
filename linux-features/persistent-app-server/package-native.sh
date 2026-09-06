#!/usr/bin/env bash
set -euo pipefail

: "${PACKAGE_ROOT:?}"
: "${PACKAGE_APP_DIR:?}"
: "${PACKAGE_NAME:?}"

for executable in codex codex-code-mode-host; do
    if [ "$executable" = "codex" ]; then
        source_path="$PACKAGE_APP_DIR/.codex-linux/features/persistent-app-server/codex-cli-wrapper"
        installed_source="/opt/$PACKAGE_NAME/.codex-linux/features/persistent-app-server/codex-cli-wrapper"
    else
        source_path="$PACKAGE_APP_DIR/resources/$executable"
        installed_source="/opt/$PACKAGE_NAME/resources/$executable"
    fi
    target_path="$PACKAGE_ROOT/usr/bin/$executable"
    [ -x "$source_path" ] || {
        echo "persistent-app-server: missing packaged executable: $source_path" >&2
        exit 1
    }
    if [ -e "$target_path" ] || [ -L "$target_path" ]; then
        echo "persistent-app-server: refusing existing package entrypoint: $target_path" >&2
        exit 1
    fi
    ln -s "$installed_source" "$target_path"
done
