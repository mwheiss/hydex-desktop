#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROFILE="${1:-${RHEL_COMPAT_PROFILE:-}}"
SOURCE_APP_DIR="${APP_DIR_OVERRIDE:-$REPO_DIR/codex-app}"
DIST_DIR="${DIST_DIR_OVERRIDE:-$REPO_DIR/dist}"
PACKAGE_NAME="${PACKAGE_NAME:-hydex-desktop}"
RUNTIME_MANIFEST="$REPO_DIR/packaging/rhel-compat/runtime-packages.json"
CACHE_DIR="${RHEL_COMPAT_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/hydex-desktop/rhel-compat}"
BUILD_TMP="${TMPDIR:-/tmp}"

info() { printf '[rhel-compat] %s\n' "$*" >&2; }
error() { printf '[rhel-compat][ERROR] %s\n' "$*" >&2; exit 1; }

download_checked() {
    local package="$1"
    local file_name="$2"
    local url="$3"
    local expected_sha="$4"
    local cached="$CACHE_DIR/$file_name"
    local temporary
    local actual_sha
    if [ ! -e "$cached" ]; then
        temporary="$(mktemp "$CACHE_DIR/.$file_name.XXXXXX")"
        if ! curl --proto '=https' --tlsv1.2 --location --fail --silent --show-error \
            --output "$temporary" "$url"; then
            rm -f -- "$temporary"
            error "failed to download $package"
        fi
        actual_sha="$(sha256sum "$temporary" | awk '{print $1}')"
        if [ "$actual_sha" != "$expected_sha" ]; then
            rm -f -- "$temporary"
            error "$package checksum mismatch: $actual_sha"
        fi
        mv -f -- "$temporary" "$cached"
    fi
    [ -f "$cached" ] && [ ! -L "$cached" ] || error "unsafe cached input: $cached"
    actual_sha="$(sha256sum "$cached" | awk '{print $1}')"
    [ "$actual_sha" = "$expected_sha" ] || error "$package cached checksum mismatch: $actual_sha"
    printf '%s\n' "$cached"
}

case "$PROFILE" in
    rhel7|rhel9) ;;
    *) error "usage: $0 <rhel7|rhel9>" ;;
esac
[ "$(uname -m)" = x86_64 ] || error "RHEL compatibility packages currently support x86_64 only"
[ -x "$SOURCE_APP_DIR/ChatGPT" ] || error "missing built Desktop app: $SOURCE_APP_DIR"
[ -x "$SOURCE_APP_DIR/resources/codex" ] || error "missing bundled Codex CLI"
for command in curl dpkg-deb node patchelf readelf rpm sha256sum tar; do
    command -v "$command" >/dev/null 2>&1 || error "required command is unavailable: $command"
done

mkdir -p "$CACHE_DIR" "$DIST_DIR" "$BUILD_TMP"
[ ! -L "$CACHE_DIR" ] || error "refusing symlinked compatibility cache: $CACHE_DIR"
work_dir="$(mktemp -d "$BUILD_TMP/codex-rhel-compat.XXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT
candidate="$work_dir/app"
sysroot="$work_dir/sysroot"
mkdir -p "$sysroot"
cp -aT --reflink=auto "$SOURCE_APP_DIR" "$candidate"

while IFS=$'\t' read -r package file_name url expected_sha; do
    [ -n "$package" ] || continue
    [ "$PROFILE" != rhel7 ] || [ "$package" != libcups2 ] || continue
    cached="$(download_checked "$package" "$file_name" "$url" "$expected_sha")"
    [ "$(dpkg-deb -f "$cached" Architecture)" = amd64 ] || error "$package has the wrong architecture"
    dpkg-deb -x "$cached" "$sysroot"
done < <(node -e '
const manifest = require(process.argv[1]);
if (manifest.schemaVersion !== 1 || manifest.architecture !== "amd64") process.exit(2);
for (const item of manifest.packages) {
  console.log([item.name, item.fileName, item.url, item.sha256].join("\t"));
}' "$RUNTIME_MANIFEST")

runtime_root="/opt/$PACKAGE_NAME/.codex-linux/rhel-compat"
dynamic_linker="$runtime_root/lib/ld-linux-x86-64.so.2"
node "$REPO_DIR/nix/elf-runtime.cjs" fix \
    --root "$candidate" \
    --arch amd64 \
    --dynamic-linker "$dynamic_linker" \
    --runtime-library-path "$runtime_root/lib" \
    --patchelf "$(command -v patchelf)" \
    --chatgpt-relocator "$REPO_DIR/nix/relocate-elf-interpreter.cjs" \
    --force-rpath true
node "$REPO_DIR/nix/elf-runtime.cjs" audit \
    --root "$candidate" \
    --arch amd64 \
    --dynamic-linker "$dynamic_linker" \
    --runtime-library-path "$runtime_root/lib" \
    --check-dependencies false \
    --check-shebangs false

mkdir -p "$candidate/.codex-linux/rhel-compat/lib" \
    "$candidate/.codex-linux/rhel-compat/gconv" \
    "$candidate/.codex-linux/rhel-compat/licenses"
cp -a "$sysroot/lib/x86_64-linux-gnu/." "$candidate/.codex-linux/rhel-compat/lib/"
find "$sysroot/usr/lib/x86_64-linux-gnu" -maxdepth 1 \
    \( -type f -o -type l \) -name 'lib*.so*' -exec \
    cp -a -t "$candidate/.codex-linux/rhel-compat/lib" {} +
cp -a "$sysroot/usr/lib/x86_64-linux-gnu/gconv/." "$candidate/.codex-linux/rhel-compat/gconv/"
cp "$sysroot/usr/share/doc/libc6/copyright" \
    "$candidate/.codex-linux/rhel-compat/licenses/libc6-copyright"
cp "$sysroot/usr/share/doc/gcc-12-base/copyright" \
    "$candidate/.codex-linux/rhel-compat/licenses/gcc-12-base-copyright"
if [ -f "$sysroot/usr/share/doc/libcups2/copyright" ]; then
    cp "$sysroot/usr/share/doc/libcups2/copyright" \
        "$candidate/.codex-linux/rhel-compat/licenses/libcups2-copyright"
fi
cp "$RUNTIME_MANIFEST" "$candidate/.codex-linux/rhel-compat/runtime-packages.json"
[ -e "$candidate/.codex-linux/rhel-compat/lib/libstdc++.so.6" ] || \
    error "private runtime is missing libstdc++.so.6"
printf 'CODEX_RHEL_COMPAT_RUNTIME=1\nGCONV_PATH=%s/gconv\n' "$runtime_root" \
    > "$candidate/.codex-linux/env.d/rhel-compat-runtime"
chmod 0644 "$candidate/.codex-linux/env.d/rhel-compat-runtime"

if [ "$PROFILE" = rhel7 ]; then
    command -v docker >/dev/null 2>&1 || error "docker is required to build the RHEL 7 CUPS compatibility library"
    IFS=$'\t' read -r cups_file cups_url cups_sha cups_image < <(node -e '
const item = require(process.argv[1]).rhel7CupsSource;
console.log([item.fileName, item.url, item.sha256, item.buildImage].join("\t"));
' "$RUNTIME_MANIFEST")
    cups_archive="$(download_checked cups "$cups_file" "$cups_url" "$cups_sha")"
    cups_source="$work_dir/cups-source"
    mkdir -p "$cups_source"
    tar -xzf "$cups_archive" -C "$cups_source" --strip-components=1
    cups_jobs="${MAX_BUILD_THREADS:-0}"
    [ "$cups_jobs" != 0 ] || cups_jobs=2
    docker run --rm \
        -e "HOST_UID=$(id -u)" \
        -e "HOST_GID=$(id -g)" \
        -e "CUPS_JOBS=$cups_jobs" \
        -v "$cups_source:/src" \
        "$cups_image" bash -euo pipefail -c '
sed -i -e "s|^mirrorlist=|#mirrorlist=|" \
  -e "s|^#baseurl=http://mirror.centos.org/centos/\$releasever|baseurl=https://vault.centos.org/7.9.2009|" \
  /etc/yum.repos.d/CentOS-*.repo
yum clean all >/dev/null
yum install -y gcc make zlib-devel >/dev/null
cd /src
./configure --prefix=/opt/codex-cups --with-components=libcups \
  --disable-dbus --disable-pam --with-tls=no --with-dnssd=no >/dev/null
make -j "$CUPS_JOBS" >/dev/null
chown -R "$HOST_UID:$HOST_GID" /src
'
    cups_library="$cups_source/cups/libcups.so.2"
    [ -f "$cups_library" ] || error "RHEL 7 CUPS build did not produce libcups.so.2"
    cups_symbols="$(readelf --dyn-syms --wide "$cups_library")"
    grep -q ' ippValidateAttributes' <<<"$cups_symbols" || \
        error "RHEL 7 CUPS library lacks ippValidateAttributes"
    grep -q ' ppdOpenFd' <<<"$cups_symbols" || \
        error "RHEL 7 CUPS library lacks ppdOpenFd"
    cups_versions="$(readelf --version-info "$cups_library")"
    if grep -Eq 'GLIBC_2\.(1[89]|[2-9][0-9])' <<<"$cups_versions"; then
        error "RHEL 7 CUPS library requires glibc newer than 2.17"
    fi
    cp "$cups_library" "$candidate/.codex-linux/rhel-compat/lib/libcups.so.2"
    cp "$cups_source/LICENSE" "$candidate/.codex-linux/rhel-compat/licenses/cups-LICENSE"
    cp "$cups_source/NOTICE" "$candidate/.codex-linux/rhel-compat/licenses/cups-NOTICE"
fi

retained="$candidate/.codex-linux/features/hydex-offload/codex"
if [ -e "$retained" ]; then
    cmp -s "$candidate/resources/codex" "$retained" || error "retained Hydex CLI differs from resources/codex"
    rm -f -- "$retained"
fi

manifest_sha="$(sha256sum "$RUNTIME_MANIFEST" | awk '{print $1}')"
node -e '
const fs = require("node:fs");
const [file, profile, manifestSha] = process.argv.slice(1);
const value = JSON.parse(fs.readFileSync(file, "utf8"));
value.rhelCompatibility = { profile, runtime: "ubuntu-22.04-glibc-2.35", manifestSha256: manifestSha, updater: false };
fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
' "$candidate/.codex-linux/build-info.json" "$PROFILE" "$manifest_sha"

package_version="${PACKAGE_VERSION:-$(date -u +%Y.%m.%d.%H%M%S)}"
case "$package_version" in
    *+*) error "PACKAGE_VERSION must not contain '+' for a compatibility build" ;;
esac
package_version="$package_version+$PROFILE"
payload="${RPM_BINARY_PAYLOAD:-}"
if [ "$PROFILE" = rhel7 ]; then
    payload="w9.gzdio"
elif [ -z "$payload" ] && [ "${MAX_BUILD_THREADS:-0}" != 0 ]; then
    payload="w19T${MAX_BUILD_THREADS}.zstdio"
fi

info "building $PROFILE package from the private glibc 2.35 runtime; updater disabled"
APP_DIR_OVERRIDE="$candidate" \
DIST_DIR_OVERRIDE="$DIST_DIR" \
PACKAGE_NAME="$PACKAGE_NAME" \
PACKAGE_VERSION="$package_version" \
PACKAGE_WITH_UPDATER=0 \
RPM_COMPAT_PROFILE="$PROFILE" \
RPM_BINARY_PAYLOAD="$payload" \
MAX_BUILD_THREADS="${MAX_BUILD_THREADS:-0}" \
    "$REPO_DIR/scripts/build-rpm.sh"

rpm_base="${package_version%%+*}"
rpm_release="${package_version#*+}"
mapfile -t artifacts < <(
    find "$DIST_DIR" -maxdepth 1 -type f \
        -name "$PACKAGE_NAME*-$rpm_base-$rpm_release*.x86_64.rpm" | LC_ALL=C sort
)
expected_count=1
[ "$PROFILE" != rhel7 ] || expected_count=2
[ "${#artifacts[@]}" -eq "$expected_count" ] || \
    error "expected $expected_count $PROFILE RPM artifact(s), found ${#artifacts[@]}"
for artifact in "${artifacts[@]}"; do
    verify_output="$(rpm -Kv "$artifact" 2>&1 || true)"
    grep -q 'Header SHA256 digest: OK' <<<"$verify_output" || \
        error "RPM header digest verification failed: $artifact"
    grep -q 'Payload SHA256 digest: OK' <<<"$verify_output" || \
        error "RPM payload digest verification failed: $artifact"
    [ "$(rpm -qp --qf '%{PAYLOADFORMAT}' "$artifact")" = cpio ] || \
        error "unexpected RPM payload format: $artifact"
    if [ "$PROFILE" = rhel7 ]; then
        [ "$(rpm -qp --qf '%{PAYLOADCOMPRESSOR}' "$artifact")" = gzip ] || \
            error "RHEL 7 RPM payload is not gzip: $artifact"
        if rpm -qp --requires "$artifact" | grep -Eq 'rpmlib\((LargeFiles|PayloadIsZstd)\)'; then
            error "RHEL 7 RPM requires an unsupported payload capability: $artifact"
        fi
    fi
    info "validated $artifact"
done
