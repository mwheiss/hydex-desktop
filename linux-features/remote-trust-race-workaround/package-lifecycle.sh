#!/bin/sh

REMOTE_TRUST_APP_DIR="${REMOTE_TRUST_APP_DIR:-/opt/hydex-desktop}"
REMOTE_TRUST_UNIT="hydex-remote-trust-race-workaround.service"
REMOTE_TRUST_MARKER="# Managed by hydex-desktop remote-trust-race-workaround v1"

codex_remote_trust_run_user() {
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

codex_remote_trust_foreach_active_user() {
    command -v getent >/dev/null 2>&1 || return 0
    command -v runuser >/dev/null 2>&1 || return 0
    command -v systemctl >/dev/null 2>&1 || return 0
    for runtime_dir in /run/user/*; do
        [ -d "$runtime_dir" ] || continue
        uid="$(basename "$runtime_dir")"
        case "$uid" in ''|*[!0-9]*|0) continue ;; esac
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

codex_remote_trust_stop_one_user() {
    codex_remote_trust_run_user "$1" "$2" "$3" "$4" \
        systemctl --user disable --now "$REMOTE_TRUST_UNIT" >/dev/null 2>&1 || true
}

codex_remote_trust_reload_one_user() {
    codex_remote_trust_run_user "$1" "$2" "$3" "$4" \
        systemctl --user daemon-reload >/dev/null 2>&1 || true
}

codex_remote_trust_remove_owned_units() {
    command -v getent >/dev/null 2>&1 || return 0
    getent passwd | while IFS=: read -r _ _ uid _ _ home _; do
        case "$uid" in ''|*[!0-9]*|0) continue ;; esac
        [ -n "$home" ] && [ "$home" != "/" ] || continue
        unit="$home/.config/systemd/user/$REMOTE_TRUST_UNIT"
        [ -f "$unit" ] && [ ! -L "$unit" ] || continue
        first_line="$(sed -n '1p' "$unit" 2>/dev/null || true)"
        [ "$first_line" = "$REMOTE_TRUST_MARKER" ] || continue
        rm -f -- "$unit"
        wants="$home/.config/systemd/user/default.target.wants/$REMOTE_TRUST_UNIT"
        [ -L "$wants" ] && rm -f -- "$wants" || true
    done
}

codex_remote_trust_remove_all_users() {
    codex_remote_trust_foreach_active_user codex_remote_trust_stop_one_user
    codex_remote_trust_remove_owned_units
    codex_remote_trust_foreach_active_user codex_remote_trust_reload_one_user
}
