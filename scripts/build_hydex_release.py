#!/usr/bin/env python3
"""Build and validate one Hydex Desktop native release from explicit inputs."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import publish_desktop_copr as copr

SCHEMA_VERSION = 1
REQUIRED_FEATURES = {"remote-mobile-control", "hydex-offload", "persistent-app-server"}
FORBIDDEN_FEATURES = {"shared-app-server-socket"}


def log_command(command: Iterable[object], cwd: Path) -> None:
    print(f"+ ({cwd}) {' '.join(str(part) for part in command)}", flush=True)


def run(
    command: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    log_command(command, cwd)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require_commands(names: Iterable[str]) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise SystemExit(f"missing required commands: {', '.join(missing)}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_version(binary: Path) -> str:
    output = run([str(binary), "--version"], binary.parent).stdout.strip()
    match = re.fullmatch(r"codex-cli (.+)", output)
    if not match:
        raise SystemExit(f"could not parse Codex version from {binary}: {output}")
    return match.group(1)


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    log_command(["git", *arguments], repo)
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=check,
        text=True,
        capture_output=True,
    )


def validate_features(config: Path) -> None:
    value = json.loads(config.read_text())
    enabled = set(value.get("enabled", []))
    missing = REQUIRED_FEATURES - enabled
    forbidden = FORBIDDEN_FEATURES & enabled
    if missing or forbidden:
        raise SystemExit(
            f"invalid release feature selection: missing={sorted(missing)}, forbidden={sorted(forbidden)}"
        )


def deb_metadata(package: Path) -> dict[str, str]:
    fields = {}
    for field in ("Package", "Version", "Architecture"):
        fields[field] = run(["dpkg-deb", "-f", str(package), field], package.parent).stdout.strip()
    if fields != {
        "Package": "chatgpt",
        "Version": fields["Version"],
        "Architecture": "amd64",
    }:
        raise SystemExit(f"unexpected upstream package metadata: {fields}")
    fields["sha256"] = sha256(package)
    return fields


def expected_artifacts(output: Path, version: str) -> dict[str, Path]:
    return {
        "pacman": output / f"hydex-desktop-{version}-1-x86_64.pkg.tar.zst",
        "fullRpm": output / f"hydex-desktop-{version}-1.x86_64.rpm",
        "rhel9Rpm": output / f"hydex-desktop-{version}-rhel9.x86_64.rpm",
        "rhel7Rpm": output / f"hydex-desktop-{version}-rhel7.x86_64.rpm",
        "rhel7CliRpm": output / f"hydex-desktop-cli-runtime-{version}-rhel7.x86_64.rpm",
    }


def validate_candidate(
    args: argparse.Namespace,
    upstream: dict[str, str],
    expected_hydex_version: str,
    expected_hydex_sha: str,
) -> dict[str, object]:
    candidate = args.candidate_dir
    build_info_path = candidate / ".codex-linux" / "build-info.json"
    patch_report_path = candidate / ".codex-linux" / "patch-report.json"
    if not build_info_path.is_file() or not patch_report_path.is_file():
        raise SystemExit(f"candidate is missing build reports: {candidate}")
    build_info = json.loads(build_info_path.read_text())
    patch_report = json.loads(patch_report_path.read_text())
    package = build_info.get("upstreamLinuxPackage", {})
    if package.get("version") != upstream["Version"] or package.get("sha256") != upstream["sha256"]:
        raise SystemExit("candidate upstream package identity differs from the explicit .deb")
    source = build_info.get("source", {})
    if source.get("dirty") is not False or not source.get("commit"):
        raise SystemExit("candidate source provenance is missing or dirty")
    if not args.validate_only:
        head = git(args.repo, "rev-parse", "HEAD").stdout.strip()
        if source["commit"] != head:
            raise SystemExit(f"candidate source commit {source['commit']} does not match HEAD {head}")
    retained = git(args.repo, "branch", "-r", "--contains", source["commit"]).stdout.strip()
    if not retained:
        raise SystemExit(f"candidate source commit has no retained remote ref: {source['commit']}")
    enabled = set(build_info.get("linuxFeatures", {}).get("enabled", []))
    if enabled != REQUIRED_FEATURES:
        raise SystemExit(f"candidate feature set is not exact: {sorted(enabled)}")
    rejected = [
        patch
        for patch in patch_report.get("patches", [])
        if patch.get("status") in {"error", "rejected"}
        or (
            patch.get("enforceWhenEnabled") is True
            and patch.get("status") in {"skipped", "skipped-disabled"}
        )
    ]
    if rejected:
        raise SystemExit(f"candidate has rejecting patch entries: {rejected}")

    active = candidate / "resources" / "codex"
    retained_binary = candidate / ".codex-linux" / "features" / "hydex-offload" / "codex"
    for binary in (active, retained_binary):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise SystemExit(f"candidate Hydex binary is missing or not executable: {binary}")
        if command_version(binary) != expected_hydex_version or sha256(binary) != expected_hydex_sha:
            raise SystemExit(f"candidate Hydex binary differs from explicit input: {binary}")
        description = run(["file", str(binary)], candidate).stdout
        if "static-pie linked" not in description:
            raise SystemExit(f"candidate Hydex binary is not static PIE: {binary}")
    help_output = run([str(active), "--help"], candidate).stdout
    if "--offload" not in help_output or "--no-offload" not in help_output:
        raise SystemExit("candidate Hydex CLI is missing offload flags")
    return {"buildInfo": build_info, "patchReport": patch_report}


def build_candidate(args: argparse.Namespace) -> None:
    if args.candidate_dir.exists():
        raise SystemExit(f"candidate already exists: {args.candidate_dir}")
    args.candidate_dir.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "TMPDIR": str(args.temp_dir),
            "CODEX_INSTALL_TRANSACTION_ACTIVE": "1",
            "CODEX_INSTALL_DIR": str(args.candidate_dir),
            "CODEX_LINUX_FEATURES_CONFIG": str(args.features_config),
            "HYDEX_CLI_BINARY": str(args.hydex_bin),
        }
    )
    run([str(args.repo / "install.sh"), str(args.upstream_deb)], args.repo, env=environment, capture=False)


def build_packages(args: argparse.Namespace) -> None:
    artifacts = expected_artifacts(args.output_dir, args.package_version)
    existing = [str(path) for path in artifacts.values() if path.exists()]
    if existing:
        raise SystemExit(f"release artifacts already exist: {existing}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    base = os.environ.copy()
    base.update(
        {
            "TMPDIR": str(args.temp_dir),
            "CODEX_LINUX_FEATURES_CONFIG": str(args.features_config),
            "APP_DIR_OVERRIDE": str(args.candidate_dir),
            "DIST_DIR_OVERRIDE": str(args.output_dir),
            "PACKAGE_VERSION": args.package_version,
            "MAX_BUILD_THREADS": str(args.max_build_threads),
        }
    )
    for target in ("pacman", "rpm", "rpm-rhel9", "rpm-rhel7"):
        environment = base.copy()
        if target in {"pacman", "rpm"}:
            environment["PACKAGE_WITH_UPDATER"] = "1"
        run(["make", target], args.repo, env=environment, capture=False)


def validate_packages(args: argparse.Namespace) -> dict[str, object]:
    artifacts = expected_artifacts(args.output_dir, args.package_version)
    for path in artifacts.values():
        if not path.is_file():
            raise SystemExit(f"missing release artifact: {path}")

    pacman_files = run(["pacman", "-Qlp", str(artifacts["pacman"])], args.output_dir).stdout
    for command in copr.EXPECTED_COMMANDS + copr.UPDATER_COMMANDS:
        if f"/usr/bin/{command}" not in pacman_files:
            raise SystemExit(f"pacman package is missing /usr/bin/{command}")

    expected = {
        "fullRpm": ("1", "cpio", "zstd", copr.EXPECTED_COMMANDS + copr.UPDATER_COMMANDS),
        "rhel9Rpm": ("rhel9", "cpio", "zstd", copr.EXPECTED_COMMANDS),
        "rhel7Rpm": ("rhel7", "cpio", "gzip", copr.EXPECTED_COMMANDS),
        "rhel7CliRpm": ("rhel7", "cpio", "gzip", ()),
    }
    for name, (release, payload_format, compressor, commands) in expected.items():
        path = artifacts[name]
        identity = copr.rpm_identity(path)
        if identity["version"] != args.package_version or identity["release"] != release:
            raise SystemExit(f"unexpected {name} identity: {identity}")
        payload = copr.rpm_query(path, ["--qf", "%{PAYLOADFORMAT}\t%{PAYLOADCOMPRESSOR}\n"]).strip()
        if payload != f"{payload_format}\t{compressor}":
            raise SystemExit(f"unexpected {name} payload: {payload}")
        files = {row[0] for row in copr.rpm_file_rows(path)}
        for command in commands:
            if f"/usr/bin/{command}" not in files:
                raise SystemExit(f"{name} is missing /usr/bin/{command}")
    rhel7_requirements = copr.rpm_lines(artifacts["rhel7Rpm"], "--requires")
    if any(value.startswith(("rpmlib(LargeFiles)", "rpmlib(PayloadIsZstd)")) for value in rhel7_requirements):
        raise SystemExit("RHEL 7 package requires unsupported RPM capabilities")
    return {
        name: {"path": str(path), "sha256": sha256(path)}
        for name, path in artifacts.items()
    }


def write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(f"{json.dumps(report, indent=2, sort_keys=True)}\n")
    temporary.replace(path)


def print_plan(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            {
                "repo": str(args.repo),
                "upstreamDeb": str(args.upstream_deb),
                "hydexBinary": str(args.hydex_bin),
                "featuresConfig": str(args.features_config),
                "candidateDir": str(args.candidate_dir),
                "outputDir": str(args.output_dir),
                "packageVersion": args.package_version,
                "artifacts": {
                    name: str(path)
                    for name, path in expected_artifacts(args.output_dir, args.package_version).items()
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


def parse_args() -> argparse.Namespace:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build one exact-input Hydex Desktop native release")
    parser.add_argument("--repo", type=Path, default=repo)
    parser.add_argument("--upstream-deb", type=Path, required=True)
    parser.add_argument("--hydex-bin", type=Path, required=True)
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--features-config", type=Path, default=repo / "linux-features" / "features.json")
    parser.add_argument("--candidate-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--temp-dir", type=Path, default=Path("/home/mheiss/.cache/hydex-build/tmp"))
    parser.add_argument("--max-build-threads", type=int, default=8)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d{6}", args.package_version):
        parser.error("--package-version must use the RPM-safe UTC form YYYY.MM.DD.HHMMSS")
    args.repo = args.repo.resolve()
    args.upstream_deb = args.upstream_deb.resolve()
    args.hydex_bin = args.hydex_bin.resolve()
    args.features_config = args.features_config.resolve()
    args.temp_dir = args.temp_dir.resolve()
    args.candidate_dir = (
        args.candidate_dir.resolve()
        if args.candidate_dir
        else args.repo / "dist-next" / f"release-{args.package_version}-app"
    )
    args.output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else args.repo / "dist" / f"release-{args.package_version}"
    )
    args.report = (
        args.report.resolve()
        if args.report
        else args.output_dir / "release-report.json"
    )
    if args.max_build_threads < 1:
        parser.error("--max-build-threads must be positive")
    return args


def main() -> None:
    args = parse_args()
    require_commands(("dpkg-deb", "file", "git", "make", "pacman", "rpm"))
    for path in (args.upstream_deb, args.hydex_bin, args.features_config):
        if not path.is_file():
            raise SystemExit(f"required input is missing: {path}")
    validate_features(args.features_config)
    if args.plan_only:
        print_plan(args)
        return
    args.temp_dir.mkdir(parents=True, exist_ok=True)
    upstream = deb_metadata(args.upstream_deb)
    hydex_version = command_version(args.hydex_bin)
    hydex_sha = sha256(args.hydex_bin)
    if not args.validate_only:
        tracked = git(args.repo, "status", "--porcelain", "--untracked-files=no").stdout.splitlines()
        if tracked:
            raise SystemExit(f"Desktop source has tracked changes: {tracked}")
        build_candidate(args)
        validate_candidate(args, upstream, hydex_version, hydex_sha)
        build_packages(args)
    candidate = validate_candidate(args, upstream, hydex_version, hydex_sha)
    artifacts = validate_packages(args)
    report = {
        "schemaVersion": SCHEMA_VERSION,
        "packageVersion": args.package_version,
        "upstream": upstream,
        "hydexVersion": hydex_version,
        "hydexSha256": hydex_sha,
        "candidate": {
            "path": str(args.candidate_dir),
            "source": candidate["buildInfo"]["source"],
            "features": candidate["buildInfo"]["linuxFeatures"]["enabled"],
        },
        "artifacts": artifacts,
        "validated": True,
    }
    write_report(args.report, report)
    print("HYDEX_DESKTOP_RELEASE_SUMMARY")
    print(f"package_version={args.package_version}")
    print(f"desktop_app_version={upstream['Version']}")
    print(f"codex_version={hydex_version}")
    print(f"candidate={args.candidate_dir}")
    print(f"output_dir={args.output_dir}")
    print(f"report={args.report}")
    print("validated=true")


if __name__ == "__main__":
    main()
