#!/usr/bin/env bash
set -euo pipefail

feature_dir="${INSTALL_DIR:?}/.codex-linux/features/hydex-offload"
target="${INSTALL_DIR:?}/resources/codex"
bundled_source="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/runtime/codex"
source="${HYDEX_CLI_BINARY:-$bundled_source}"
temporary_target="${target}.hydex.$$"

cleanup() {
    rm -f -- "$temporary_target"
}
trap cleanup EXIT

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

codex_version() {
    "$1" --version 2>&1 | awk '$1 == "codex-cli" { print $2; exit }'
}

validate_architecture() {
    local file_output="$1"
    case "${ARCH:?}" in
        amd64|x86_64)
            [[ "$file_output" == *"x86-64"* ]] || fail "Hydex CLI is not an x86-64 binary: $file_output"
            ;;
        arm64|aarch64)
            [[ "$file_output" == *"aarch64"* || "$file_output" == *"ARM64"* ]] || \
                fail "Hydex CLI is not an arm64 binary: $file_output"
            ;;
        *)
            fail "unsupported Hydex CLI architecture: $ARCH"
            ;;
    esac
}

validate_hydex_binary() {
    local binary="$1"
    local expected_version="$2"
    local actual_version
    local file_output
    local help_output

    [ -x "$binary" ] || fail "Hydex CLI is missing or not executable: $binary"
    file_output="$(file "$binary")"
    [[ "$file_output" == *"statically linked"* || "$file_output" == *"static-pie linked"* ]] || \
        fail "Hydex CLI must be statically linked: $file_output"
    validate_architecture "$file_output"

    actual_version="$(codex_version "$binary")"
    [ -n "$actual_version" ] || fail "could not read Hydex CLI version: $binary"
    [ "$actual_version" = "$expected_version" ] || \
        fail "Hydex CLI version $actual_version does not match bundled Codex $expected_version"

    help_output="$("$binary" --help 2>&1)"
    [[ "$help_output" == *"--offload"* && "$help_output" == *"--no-offload"* ]] || \
        fail "Hydex CLI help is missing --offload or --no-offload: $binary"
}

[ -x "$target" ] || fail "bundled Codex CLI is missing or not executable: $target"
expected_version="$(codex_version "$target")"
[ -n "$expected_version" ] || fail "could not read bundled Codex CLI version: $target"
validate_hydex_binary "$source" "$expected_version"

mkdir -p "$feature_dir"
install -m 0755 "$source" "$feature_dir/codex"
install -m 0755 "$feature_dir/codex" "$temporary_target"
validate_hydex_binary "$temporary_target" "$expected_version"
mv -f -- "$temporary_target" "$target"

original_sha256="$(sha256sum "${CODEX_UPSTREAM_APP_DIR:?}/resources/codex" | awk '{print $1}')"
hydex_sha256="$(sha256sum "$target" | awk '{print $1}')"
{
    printf 'codex_version=%s\n' "$expected_version"
    printf 'original_codex_sha256=%s\n' "$original_sha256"
    printf 'hydex_codex_sha256=%s\n' "$hydex_sha256"
} > "$feature_dir/build-info"
