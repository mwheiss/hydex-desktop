#!/bin/sh

PERSISTENT_APP_DIR="${PERSISTENT_APP_DIR:-/opt/hydex-desktop}"
PERSISTENT_CLEANUP="$PERSISTENT_APP_DIR/.codex-linux/features/persistent-app-server/uninstall-cleanup.py"
PERSISTENT_UNIT="codex-remote-control.service"

codex_persistent_foreach_active_user() {
    if ! command -v getent >/dev/null 2>&1 ||
       ! command -v runuser >/dev/null 2>&1 ||
       ! command -v systemctl >/dev/null 2>&1; then
        return
    fi

    for runtime_dir in /run/user/*; do
        [ -d "$runtime_dir" ] || continue
        uid="$(basename "$runtime_dir")"
        case "$uid" in
            ''|*[!0-9]*|0) continue ;;
        esac
        bus="$runtime_dir/bus"
        [ -S "$bus" ] || continue
        account="$(getent passwd "$uid" || true)"
        [ -n "$account" ] || continue
        user_name="$(printf '%s\n' "$account" | cut -d: -f1)"
        home="$(printf '%s\n' "$account" | cut -d: -f6)"
        [ -n "$user_name" ] && [ -d "$home" ] || continue
        "$@" "$user_name" "$home" "$runtime_dir" "$bus"
    done
}

codex_persistent_run_user() {
    user_name="$1"
    home="$2"
    runtime_dir="$3"
    bus="$4"
    shift 4
    runuser -u "$user_name" -- env \
        HOME="$home" \
        USER="$user_name" \
        LOGNAME="$user_name" \
        XDG_RUNTIME_DIR="$runtime_dir" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$bus" \
        PATH="/usr/local/bin:/usr/bin:/bin" \
        "$@"
}

codex_persistent_stop_one_user() {
    codex_persistent_run_user "$1" "$2" "$3" "$4" \
        systemctl --user disable --now "$PERSISTENT_UNIT" >/dev/null 2>&1 || true
}

codex_persistent_reload_one_user() {
    codex_persistent_run_user "$1" "$2" "$3" "$4" \
        systemctl --user daemon-reload >/dev/null 2>&1 || true
}

codex_persistent_remove_all_users() {
    codex_persistent_foreach_active_user codex_persistent_stop_one_user
    if [ -f "$PERSISTENT_CLEANUP" ] && command -v python3 >/dev/null 2>&1; then
        python3 "$PERSISTENT_CLEANUP" --app-dir "$PERSISTENT_APP_DIR" || true
    fi
    codex_persistent_foreach_active_user codex_persistent_reload_one_user
}
