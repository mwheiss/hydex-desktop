#!/usr/bin/env python3
"""Build, publish, and validate prebuilt Hydex Desktop COPR packages.

The source replay and accepted native package build remain outside this script. Once those
artifacts exist, this script owns the deterministic, non-creative portion of publication:

* reconstruct the full, RHEL 8/9, and split RHEL 7 SRPM inputs;
* rebuild each SRPM locally and compare it with the accepted native package;
* submit COPR tiers sequentially so one package build cannot cancel another;
* download every live RPM directly from its indexed result URL;
* compare normalized live metadata and payloads; and
* run the matching offline UBI smoke tests.
"""

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

SCHEMA_VERSION = 1
PACKAGE = "hydex-desktop"
EXPECTED_COMMANDS = (
    "codex",
    "hydex",
    "codex-code-mode-host",
    "hydex-code-mode-host",
    "codex-desktop",
    "hydex-desktop",
)
UPDATER_COMMANDS = ("codex-update-manager", "hydex-update-manager")


@dataclasses.dataclass(frozen=True)
class Tier:
    name: str
    template: str
    release: str
    chroots: tuple[str, ...]
    source_payload: str
    binary_payload: str
    split_cli: bool = False
    updater: bool = False

    def main_name(self, version: str, dist: str = "") -> str:
        return f"{PACKAGE}-{version}-{self.release}{dist}.x86_64.rpm"

    def cli_name(self, version: str, dist: str = "") -> str:
        if not self.split_cli:
            raise ValueError(f"{self.name} has no split CLI package")
        return f"{PACKAGE}-cli-runtime-{version}-{self.release}{dist}.x86_64.rpm"

    def srpm_name(self, version: str, dist: str = "") -> str:
        return f"{PACKAGE}-{version}-{self.release}{dist}.src.rpm"


TIERS = (
    Tier(
        name="rhel9",
        template="prebuilt-rhel9.spec.in",
        release="rhel9",
        chroots=("rhel-8-x86_64", "epel-8-x86_64", "rhel-9-x86_64", "epel-9-x86_64"),
        source_payload="w19T8.zstdio",
        binary_payload="w19T8.zstdio",
    ),
    Tier(
        name="rhel7",
        template="prebuilt-rhel7.spec.in",
        release="rhel7",
        chroots=("rhel-7-x86_64", "epel-7-x86_64"),
        source_payload="w9.gzdio",
        binary_payload="w9.gzdio",
        split_cli=True,
    ),
    Tier(
        name="full",
        template="prebuilt-full.spec.in",
        release="1",
        chroots=("rhel-10-x86_64", "epel-10-x86_64"),
        source_payload="w19T8.zstdio",
        binary_payload="w19T8.zstdio",
        updater=True,
    ),
)


def log_command(command: Iterable[object], cwd: Path) -> None:
    rendered = " ".join(str(part) for part in command)
    print(f"+ ({cwd}) {rendered}", flush=True)


def run(
    command: list[str],
    cwd: Path,
    *,
    capture: bool = True,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess:
    log_command(command, cwd)
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=input_bytes is None,
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


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(f"{json.dumps(value, indent=2, sort_keys=True)}\n")
    temporary.replace(path)


def rpm_query(path: Path, arguments: list[str]) -> str:
    result = run(["rpm", "-qp", *arguments, str(path)], path.parent)
    return result.stdout


def rpm_lines(path: Path, option: str) -> list[str]:
    return sorted(line for line in rpm_query(path, [option]).splitlines() if line)


def rpm_identity(path: Path) -> dict[str, str]:
    output = rpm_query(path, ["--qf", "%{NAME}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\n"])
    name, version, release, arch = output.strip().split("\t")
    return {"name": name, "version": version, "release": release, "arch": arch}


def rpm_file_rows(path: Path) -> list[tuple[str, str, str, str, str]]:
    query = "[%{FILENAMES}\\t%{FILEMODES:octal}\\t%{LONGFILESIZES}\\t%{FILEDIGESTS}\\t%{FILELINKTOS}\\n]"
    rows = []
    for line in rpm_query(path, ["--qf", query]).splitlines():
        fields = line.split("\t")
        if len(fields) != 5:
            raise SystemExit(f"malformed RPM file row in {path}: {line}")
        rows.append(tuple(fields))
    return rows


def normalized_manifest(path: Path) -> list[tuple[str, str, str, str, str]]:
    normalized = []
    for name, mode, size, digest, target in rpm_file_rows(path):
        if name.startswith("/usr/lib/.build-id"):
            continue
        if mode.startswith("4"):
            size = "0"
        normalized.append((name, mode, size, digest, target))
    return sorted(normalized)


def normalized_requires(path: Path) -> list[str]:
    values = []
    for value in rpm_lines(path, "--requires"):
        if value.startswith("rpmlib("):
            continue
        values.append(value.replace("-rhel7.el7_9", "-rhel7"))
    return sorted(values)


def normalized_provides(path: Path) -> list[str]:
    own = re.compile(r"^hydex-desktop(?:\(x86-64\))? = ")
    return [value for value in rpm_lines(path, "--provides") if not own.match(value)]


def source_manifest(path: Path) -> list[tuple[str, str, str]]:
    query = "[%{FILENAMES}\\t%{LONGFILESIZES}\\t%{FILEDIGESTS}\\n]"
    rows = []
    for line in rpm_query(path, ["--qf", query]).splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            raise SystemExit(f"malformed source RPM row in {path}: {line}")
        rows.append(tuple(fields))
    return sorted(rows)


def extract_rpms(packages: Iterable[Path], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for package in packages:
        log_command(["rpm2cpio", package, "|", "cpio", "-idm", "--quiet"], destination)
        rpm2cpio = subprocess.Popen(["rpm2cpio", str(package)], stdout=subprocess.PIPE)
        assert rpm2cpio.stdout is not None
        cpio = subprocess.run(
            ["cpio", "-idm", "--quiet"],
            cwd=destination,
            stdin=rpm2cpio.stdout,
            capture_output=True,
            check=False,
        )
        rpm2cpio.stdout.close()
        rpm_status = rpm2cpio.wait()
        if rpm_status != 0 or cpio.returncode != 0:
            raise SystemExit(f"failed to extract {package}: {cpio.stderr.decode(errors='replace')}")


def verify_extracted(packages: Iterable[Path], destination: Path) -> None:
    for package in packages:
        for name, *_ in rpm_file_rows(package):
            path = destination / name.removeprefix("/")
            if not path.exists() and not path.is_symlink():
                raise SystemExit(f"{package} payload path was not extracted: {name}")


def extract_member(package: Path, member: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    log_command(["rpm2cpio", package, "|", "cpio", "--to-stdout", member], destination.parent)
    rpm2cpio = subprocess.Popen(["rpm2cpio", str(package)], stdout=subprocess.PIPE)
    assert rpm2cpio.stdout is not None
    with destination.open("wb") as output:
        cpio = subprocess.run(
            ["cpio", "-i", "--quiet", "--to-stdout", member],
            stdin=rpm2cpio.stdout,
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    rpm2cpio.stdout.close()
    rpm_status = rpm2cpio.wait()
    if rpm_status != 0 or cpio.returncode != 0:
        raise SystemExit(f"failed to extract {member} from {package}")
    return destination


def native_paths(native_dir: Path, tier: Tier, version: str) -> tuple[Path, ...]:
    main = native_dir / tier.main_name(version)
    if tier.split_cli:
        return (native_dir / tier.cli_name(version), main)
    return (main,)


def validate_native_packages(native_dir: Path, version: str, work: Path) -> dict[str, object]:
    artifacts: dict[str, object] = {}
    for tier in TIERS:
        packages = native_paths(native_dir, tier, version)
        for package in packages:
            if not package.is_file():
                raise SystemExit(f"missing native package: {package}")
        main = packages[-1]
        identity = rpm_identity(main)
        if identity["name"] != PACKAGE or identity["version"] != version or identity["arch"] != "x86_64":
            raise SystemExit(f"unexpected native RPM identity: {identity}")
        files = {row[0]: row for row in rpm_file_rows(main)}
        required = EXPECTED_COMMANDS + (UPDATER_COMMANDS if tier.updater else ())
        for command in required:
            if f"/usr/bin/{command}" not in files:
                raise SystemExit(f"{main} is missing /usr/bin/{command}")
        if not tier.updater:
            for command in UPDATER_COMMANDS:
                if f"/usr/bin/{command}" in files:
                    raise SystemExit(f"{main} unexpectedly contains /usr/bin/{command}")
        artifacts[tier.name] = {
            "native": [{"path": str(path), "sha256": sha256(path)} for path in packages],
        }

    full = native_paths(native_dir, TIERS[-1], version)[0]
    build_info_path = extract_member(
        full,
        "./opt/hydex-desktop/.codex-linux/build-info.json",
        work / "build-info.json",
    )
    build_info = json.loads(build_info_path.read_text())
    source = build_info.get("source", {})
    if source.get("dirty") is not False or not source.get("commit"):
        raise SystemExit("native full RPM does not contain clean source provenance")
    artifacts["buildInfo"] = build_info

    codex = extract_member(full, "./opt/hydex-desktop/resources/codex", work / "codex")
    codex.chmod(0o755)
    version_output = run([str(codex), "--version"], work).stdout.strip()
    match = re.fullmatch(r"codex-cli (.+)", version_output)
    if not match:
        raise SystemExit(f"could not read bundled Codex version: {version_output}")
    help_output = run([str(codex), "--help"], work).stdout
    if "--offload" not in help_output or "--no-offload" not in help_output:
        raise SystemExit("bundled Hydex CLI is missing offload flags")
    artifacts["codexVersion"] = match.group(1)
    artifacts["codexSha256"] = sha256(codex)
    return artifacts


def render_spec(template: Path, destination: Path, version: str) -> None:
    date = dt.datetime.now(dt.UTC).strftime("%a %b %d %Y")
    rendered = template.read_text().replace("@VERSION@", version).replace("@CHANGELOG_DATE@", date)
    if "@VERSION@" in rendered or "@CHANGELOG_DATE@" in rendered:
        raise SystemExit(f"unresolved token in {template}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered)


def create_payload(
    tier: Tier,
    packages: tuple[Path, ...],
    output: Path,
    version: str,
    epoch: int,
    temp_root: Path,
) -> list[Path]:
    extract_dir = temp_root / f"extract-{tier.name}"
    extract_rpms(packages, extract_dir)
    verify_extracted(packages, extract_dir)
    sources = output / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    common = [
        "tar",
        "--sort=name",
        f"--mtime=@{epoch}",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        "--format=gnu",
    ]
    if tier.split_cli:
        cli = sources / f"{PACKAGE}-{version}-rhel7-cli-payload.tar.gz"
        main = sources / f"{PACKAGE}-{version}-rhel7-main-payload.tar.gz"
        run(common + ["-I", "gzip -9n", "-cf", str(cli), "./opt/hydex-desktop/resources/codex"], extract_dir, capture=False)
        run(
            common
            + [
                "--exclude=./opt/hydex-desktop/resources/codex",
                "-I",
                "gzip -9n",
                "-cf",
                str(main),
                ".",
            ],
            extract_dir,
            capture=False,
        )
        return [main, cli]
    suffix = "1" if tier.updater else "rhel9"
    payload = sources / f"{PACKAGE}-{version}-{suffix}-x86_64-payload.tar.zst"
    run(common + ["-I", "zstd -19 -T8", "-cf", str(payload), "."], extract_dir, capture=False)
    return [payload]


def rpmbuild_tree(path: Path) -> None:
    for name in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS", "tmp"):
        (path / name).mkdir(parents=True, exist_ok=True)


def build_srpm(tier: Tier, spec: Path, payloads: list[Path], output: Path, version: str) -> tuple[Path, tuple[Path, ...]]:
    build = output / f"rpmbuild-{tier.name}"
    rpmbuild_tree(build)
    copied_spec = build / "SPECS" / f"{PACKAGE}.spec"
    shutil.copy2(spec, copied_spec)
    for payload in payloads:
        shutil.copy2(payload, build / "SOURCES" / payload.name)
    command = [
        "rpmbuild",
        "-bs",
        "--define",
        f"_topdir {build}",
        "--define",
        f"_tmppath {build / 'tmp'}",
        "--define",
        f"_source_payload {tier.source_payload}",
        str(copied_spec),
    ]
    if tier.split_cli:
        command[1:1] = ["--define", "_rpmformat 4"]
    run(command, output, capture=False)
    srpms = list((build / "SRPMS").glob("*.src.rpm"))
    if len(srpms) != 1:
        raise SystemExit(f"expected one {tier.name} SRPM, found {srpms}")
    canonical_srpm = output / "srpms" / srpms[0].name
    canonical_srpm.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(srpms[0], canonical_srpm)

    rebuild = output / f"rebuild-{tier.name}"
    rpmbuild_tree(rebuild)
    command = [
        "rpmbuild",
        "--rebuild",
        "--nodeps",
        "--define",
        f"_topdir {rebuild}",
        "--define",
        f"_tmppath {rebuild / 'tmp'}",
        "--define",
        f"_binary_payload {tier.binary_payload}",
        str(canonical_srpm),
    ]
    if tier.split_cli:
        command[3:3] = ["--define", "_rpmformat 4", "--define", "_source_payload w9.gzdio"]
    run(command, output, capture=False)
    rebuilt = tuple(sorted((rebuild / "RPMS" / "x86_64").glob("*.rpm")))
    expected = 2 if tier.split_cli else 1
    if len(rebuilt) != expected:
        raise SystemExit(f"expected {expected} rebuilt {tier.name} RPMs, found {rebuilt}")
    return canonical_srpm, rebuilt


def compare_package_sets(native: tuple[Path, ...], rebuilt: tuple[Path, ...], tier: Tier) -> None:
    native_by_name = {rpm_identity(path)["name"]: path for path in native}
    rebuilt_by_name = {rpm_identity(path)["name"]: path for path in rebuilt}
    if native_by_name.keys() != rebuilt_by_name.keys():
        raise SystemExit(f"{tier.name} rebuilt package names differ from native packages")
    for name, source in native_by_name.items():
        candidate = rebuilt_by_name[name]
        if normalized_manifest(source) != normalized_manifest(candidate):
            raise SystemExit(f"{tier.name} file manifest differs for {name}")
        if normalized_requires(source) != normalized_requires(candidate):
            raise SystemExit(f"{tier.name} requirements differ for {name}")
        if normalized_provides(source) != normalized_provides(candidate):
            raise SystemExit(f"{tier.name} providers differ for {name}")
        for option in ("--conflicts", "--obsoletes"):
            if rpm_lines(source, option) != rpm_lines(candidate, option):
                raise SystemExit(f"{tier.name} {option} differs for {name}")
        source_recommends = rpm_lines(source, "--recommends")
        candidate_recommends = rpm_lines(candidate, "--recommends")
        if tier.split_cli and name == PACKAGE:
            if source_recommends != ["kdialog", "zenity"] or candidate_recommends:
                raise SystemExit("RHEL 7 weak recommendation omission is not the documented difference")
        elif source_recommends != candidate_recommends:
            raise SystemExit(f"{tier.name} recommendations differ for {name}")
        if rpm_query(source, ["--scripts"]) != rpm_query(candidate, ["--scripts"]):
            raise SystemExit(f"{tier.name} scriptlets differ for {name}")


def prepare(args: argparse.Namespace, report: dict[str, object]) -> None:
    output = args.output_dir
    if output.exists():
        raise SystemExit(f"output directory already exists: {output}; use --resume after a completed prepare")
    output.mkdir(parents=True)
    temp_base = args.temp_dir
    temp_base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hydex-desktop-copr-", dir=temp_base) as temporary:
        temp_root = Path(temporary)
        native = validate_native_packages(args.native_dir, args.version, temp_root)
        source = native["buildInfo"]["source"]
        commit = source["commit"]
        branches = run(["git", "branch", "-r", "--contains", commit], args.repo).stdout.strip()
        if not branches:
            raise SystemExit(f"native package commit is not retained on a remote ref: {commit}")
        epoch = int(run(["git", "show", "-s", "--format=%ct", commit], args.repo).stdout.strip())
        report.update(
            {
                "schemaVersion": SCHEMA_VERSION,
                "version": args.version,
                "sourceCommit": commit,
                "codexVersion": native["codexVersion"],
                "codexSha256": native["codexSha256"],
                "native": native,
                "tiers": {},
            }
        )
        for tier in TIERS:
            template = args.repo / "packaging" / "copr" / tier.template
            spec = output / "specs" / f"{PACKAGE}-{args.version}-{tier.name}.spec"
            render_spec(template, spec, args.version)
            native_packages = native_paths(args.native_dir, tier, args.version)
            payloads = create_payload(tier, native_packages, output, args.version, epoch, temp_root)
            srpm, rebuilt = build_srpm(tier, spec, payloads, output, args.version)
            compare_package_sets(native_packages, rebuilt, tier)
            report["tiers"][tier.name] = {
                "spec": str(spec),
                "payloads": [{"path": str(path), "sha256": sha256(path)} for path in payloads],
                "srpm": {"path": str(srpm), "sha256": sha256(srpm)},
                "rebuilt": [{"path": str(path), "sha256": sha256(path)} for path in rebuilt],
                "chroots": list(tier.chroots),
            }
        report["prepared"] = True
        write_json(output / "report.json", report)


def parse_build_id(output: str) -> int:
    matches = re.findall(r"(?:coprs/build/|Created builds:\s*)(\d+)", output)
    if not matches:
        raise SystemExit(f"could not parse COPR build id from:\n{output}")
    return int(matches[-1])


def copr_status(build_id: int, repo: Path) -> str:
    return run(["copr-cli", "status", str(build_id)], repo).stdout.strip()


def publish_tiers(
    args: argparse.Namespace,
    report: dict[str, object],
    runner: Callable[..., subprocess.CompletedProcess] = run,
) -> None:
    builds = report.setdefault("builds", {})
    for tier in TIERS:
        tier_report = report["tiers"][tier.name]
        existing = builds.get(tier.name)
        if existing:
            build_id = int(existing["id"])
            status = copr_status(build_id, args.repo)
            if status == "succeeded":
                continue
            if status not in {"running", "pending", "starting", "importing", "waiting"}:
                existing = None
        if not existing:
            command = [
                "copr-cli",
                "build",
                args.project,
                tier_report["srpm"]["path"],
            ]
            for chroot in tier.chroots:
                command.extend(["--chroot", chroot])
            command.extend(["--enable-net", "off", "--nowait"])
            result = runner(command, args.repo)
            output = f"{result.stdout}\n{result.stderr}"
            build_id = parse_build_id(output)
            builds[tier.name] = {"id": build_id, "status": "submitted"}
            write_json(args.output_dir / "report.json", report)
        runner(["copr-cli", "watch-build", str(build_id)], args.repo, capture=False)
        status = copr_status(build_id, args.repo)
        if status != "succeeded":
            raise SystemExit(f"COPR build {build_id} finished with status {status}")
        builds[tier.name]["status"] = status
        write_json(args.output_dir / "report.json", report)


def download_file(url: str, destination: Path, repo: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(f"{destination.suffix}.part")
    run(
        ["curl", "--fail", "--location", "--retry", "3", url, "--output", str(partial)],
        repo,
        capture=False,
    )
    partial.replace(destination)


def download_chroot(args: argparse.Namespace, tier: Tier, build_id: int, chroot: str) -> list[Path]:
    base = f"https://download.copr.fedorainfracloud.org/results/{args.project}/{chroot}/{build_id}-{PACKAGE}"
    destination = args.output_dir / "readback" / chroot
    results_path = destination / "results.json"
    download_file(f"{base}/results.json", results_path, args.repo)
    results = json.loads(results_path.read_text())
    files = []
    for package in results.get("packages", []):
        filename = "{name}-{version}-{release}.{arch}.rpm".format(**package)
        path = destination / filename
        download_file(f"{base}/{filename}", path, args.repo)
        files.append(path)
    expected = 3 if tier.split_cli else 2
    if len(files) != expected:
        raise SystemExit(f"{chroot} expected {expected} RPMs, downloaded {files}")
    return files


def validate_live_set(tier: Tier, rebuilt: tuple[Path, ...], srpm: Path, live: list[Path]) -> None:
    live_binary = tuple(path for path in live if not path.name.endswith(".src.rpm"))
    live_source = [path for path in live if path.name.endswith(".src.rpm")]
    if len(live_source) != 1:
        raise SystemExit(f"expected one live source RPM, found {live_source}")
    compare_package_sets(rebuilt, live_binary, tier)
    if source_manifest(srpm) != source_manifest(live_source[0]):
        raise SystemExit(f"{tier.name} live source payload differs")
    if tier.split_cli:
        for package in live_binary:
            requirements = rpm_lines(package, "--requires")
            forbidden = ("rpmlib(LargeFiles)", "rpmlib(PayloadIsZstd)")
            if any(value.startswith(forbidden) for value in requirements):
                raise SystemExit(f"RHEL 7 package has unsupported requirement: {package}")


def ubi_image(chroot: str) -> str:
    major = chroot.split("-", 2)[1]
    if major == "7":
        return "registry.access.redhat.com/ubi7/ubi:7.9"
    if major == "10":
        return "registry.access.redhat.com/ubi10/ubi:10.0"
    return f"registry.access.redhat.com/ubi{major}/ubi:latest"


def smoke_live(args: argparse.Namespace, tier: Tier, chroot: str, files: list[Path], codex_version: str, codex_sha: str) -> None:
    live = args.output_dir / "readback" / chroot
    binary = sorted(path.name for path in files if not path.name.endswith(".src.rpm"))
    install = " ".join(f"/rpms/{name}" for name in binary)
    updater = (
        "test \"$(readlink /usr/bin/hydex-update-manager)\" = codex-update-manager; "
        "codex-update-manager --help >/tmp/codex-updater; "
        "hydex-update-manager --help >/tmp/hydex-updater; "
        if tier.updater
        else "test ! -e /usr/bin/codex-update-manager; test ! -e /usr/bin/hydex-update-manager; "
    )
    rhel7 = (
        "test \"$(rpm --version)\" = \"RPM version 4.11.3\"; "
        if tier.split_cli
        else ""
    )
    script = (
        "set -e; "
        f"{rhel7}rpm -ivh --nodeps {install} >/tmp/install.log; "
        "test \"$(readlink /usr/bin/codex-desktop)\" = hydex-desktop; "
        "test \"$(readlink /usr/bin/hydex)\" = codex; "
        "test \"$(readlink /usr/bin/hydex-code-mode-host)\" = codex-code-mode-host; "
        f"test \"$(/opt/hydex-desktop/resources/codex --version)\" = \"codex-cli {codex_version}\"; "
        "/opt/hydex-desktop/resources/codex --help | grep -q -- --offload; "
        "/opt/hydex-desktop/resources/codex --help | grep -q -- --no-offload; "
        f"test \"$(sha256sum /opt/hydex-desktop/resources/codex | awk '{{print $1}}')\" = {codex_sha}; "
        "codex-code-mode-host --help >/tmp/codex-host; "
        "hydex-code-mode-host --help >/tmp/hydex-host; "
        f"{updater}printf '{chroot} smoke PASS\\n'"
    )
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{live}:/rpms:ro",
            ubi_image(chroot),
            "bash",
            "-lc",
            script,
        ],
        args.repo,
        capture=False,
    )


def readback(args: argparse.Namespace, report: dict[str, object]) -> None:
    records = report.setdefault("readback", {})
    for tier in TIERS:
        build = report["builds"].get(tier.name)
        if not build or build.get("status") != "succeeded":
            raise SystemExit(f"{tier.name} has no successful COPR build")
        tier_report = report["tiers"][tier.name]
        rebuilt = tuple(Path(item["path"]) for item in tier_report["rebuilt"])
        srpm = Path(tier_report["srpm"]["path"])
        for chroot in tier.chroots:
            files = download_chroot(args, tier, int(build["id"]), chroot)
            validate_live_set(tier, rebuilt, srpm, files)
            if not args.skip_container_smoke:
                smoke_live(args, tier, chroot, files, report["codexVersion"], report["codexSha256"])
            records[chroot] = [
                {"path": str(path), "sha256": sha256(path), "identity": rpm_identity(path)}
                for path in files
            ]
            write_json(args.output_dir / "report.json", report)

    package = run(
        ["copr-cli", "get-package", args.project, "--name", PACKAGE, "--output-format", "json"],
        args.repo,
    )
    package_state = json.loads(package.stdout)
    if package_state.get("source_type") != "upload":
        raise SystemExit(f"COPR package source type is not upload: {package_state.get('source_type')}")
    report["coprPackage"] = package_state
    report["validated"] = True
    write_json(args.output_dir / "report.json", report)


def cleanup_intermediates(output: Path, report: dict[str, object]) -> None:
    targets = [output / "sources"]
    targets.extend(output / f"rpmbuild-{tier.name}" for tier in TIERS)
    targets.extend(output / f"rebuild-{tier.name}" for tier in TIERS)
    for target in targets:
        if target.exists():
            shutil.rmtree(target)
    report["intermediatesRetained"] = False
    write_json(output / "report.json", report)


def print_plan(args: argparse.Namespace) -> None:
    plan = {
        "version": args.version,
        "nativeDir": str(args.native_dir),
        "outputDir": str(args.output_dir),
        "project": args.project,
        "tiers": [
            {
                "name": tier.name,
                "native": [str(path) for path in native_paths(args.native_dir, tier, args.version)],
                "chroots": list(tier.chroots),
            }
            for tier in TIERS
        ],
    }
    print(json.dumps(plan, indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Prepare, sequentially publish, download, and validate Hydex Desktop COPR tiers"
    )
    parser.add_argument("--repo", type=Path, default=repo)
    parser.add_argument("--native-dir", type=Path, default=repo / "dist")
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--temp-dir",
        type=Path,
        default=Path("/home/mheiss/.cache/hydex-build/tmp"),
        help="persistent filesystem for large extraction/build temporaries",
    )
    parser.add_argument("--project", default="mheiss/hydex")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--skip-container-smoke", action="store_true")
    parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="retain extracted payload and local rebuild trees after successful publication",
    )
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.native_dir = args.native_dir.resolve()
    args.temp_dir = args.temp_dir.resolve()
    args.output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else args.repo / "dist" / "copr" / f"publish-{args.version}"
    )
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d{6}", args.version):
        parser.error("--version must be an RPM-safe UTC version such as 2026.09.07.104041")
    return args


def main() -> None:
    args = parse_args()
    require_commands(("cpio", "curl", "git", "rpm", "rpm2cpio", "rpmbuild", "tar", "zstd"))
    if args.publish:
        require_commands(("copr-cli",))
        if not args.skip_container_smoke:
            require_commands(("docker",))
    if args.plan_only:
        print_plan(args)
        return

    report_path = args.output_dir / "report.json"
    if args.resume:
        if not report_path.is_file():
            raise SystemExit(f"cannot resume without {report_path}")
        report = json.loads(report_path.read_text())
        if report.get("version") != args.version or not report.get("prepared"):
            raise SystemExit("resume report does not describe a completed prepare phase")
    else:
        report = {}
        prepare(args, report)
    if args.publish:
        publish_tiers(args, report)
        readback(args, report)
        if not args.keep_intermediates:
            cleanup_intermediates(args.output_dir, report)

    print("HYDEX_DESKTOP_COPR_SUMMARY")
    print(f"version={args.version}")
    print(f"output_dir={args.output_dir}")
    print(f"prepared={str(bool(report.get('prepared'))).lower()}")
    print(f"published={str(bool(report.get('builds'))).lower()}")
    print(f"validated={str(bool(report.get('validated'))).lower()}")
    for name, build in report.get("builds", {}).items():
        print(f"build_{name}={build['id']}:{build['status']}")


if __name__ == "__main__":
    main()
