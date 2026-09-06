#!/usr/bin/env python3

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path, expected: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "hydex-desktop-copr/1"})
    with (
        urllib.request.urlopen(request, timeout=120) as response,
        destination.open("wb") as output,
    ):
        shutil.copyfileobj(response, output)
    actual = sha256(destination)
    if actual != expected:
        raise SystemExit(f"download checksum mismatch for {url}: {actual}")


def materialize(
    url: str, destination: Path, expected: str, override: Path | None
) -> None:
    if override is None:
        download(url, destination, expected)
        return
    source = override.resolve()
    if sha256(source) != expected:
        raise SystemExit(f"local source checksum mismatch: {source}")
    shutil.copy2(source, destination)


def make_topdir(root: Path) -> None:
    for name in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS", "tmp"):
        (root / name).mkdir(parents=True, exist_ok=True)


def render(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(key, value)
    unresolved = sorted(set(re.findall(r"@[A-Z0-9_]+@", rendered)))
    if unresolved:
        raise SystemExit(f"unresolved spec placeholders: {unresolved}")
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--spec")
    parser.add_argument("--upstream-deb", type=Path)
    parser.add_argument("--hydex-archive", type=Path)
    args = parser.parse_args()

    copr_dir = Path(__file__).resolve().parent
    repo = copr_dir.parent
    manifest = json.loads((copr_dir / "upstream-artifact.json").read_text())
    if manifest.get("schemaVersion") != 1:
        raise SystemExit("unsupported upstream artifact manifest")

    version = str(manifest["version"])
    arch = str(manifest["architecture"])
    if arch != "amd64":
        raise SystemExit(f"COPR currently requires amd64, got {arch}")
    rpm_version = re.sub(r"[^0-9A-Za-z._]", "_", version)
    rpm_release = "0.1.dummy" if manifest.get("dummy") is True else "1"
    package_license = (
        "MIT"
        if manifest.get("dummy") is True
        else "Proprietary and LGPLv2+ and GPLv3+ with exceptions and ASL 2.0"
    )
    commit = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    source_root = f"hydex-desktop-{commit[:12]}"
    source_archive = f"{source_root}.tar.gz"
    upstream_deb = urllib.parse.unquote(
        Path(urllib.parse.urlparse(manifest["url"]).path).name
    )
    hydex_url = manifest["hydexArtifact"]["url"]
    hydex_archive = Path(urllib.parse.urlparse(hydex_url).path).name

    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hydex-desktop-copr-") as temp:
        topdir = Path(temp)
        make_topdir(topdir)
        sources = topdir / "SOURCES"
        run(
            [
                "git",
                "archive",
                "--format=tar.gz",
                f"--prefix={source_root}/",
                f"--output={sources / source_archive}",
                "HEAD",
            ],
            repo,
        )
        materialize(
            manifest["url"],
            sources / upstream_deb,
            manifest["sha256"],
            args.upstream_deb,
        )
        materialize(
            hydex_url,
            sources / hydex_archive,
            manifest["hydexArtifact"]["sha256"],
            args.hydex_archive,
        )
        shutil.copy2(copr_dir / "upstream-artifact.json", sources)
        shutil.copy2(copr_dir / "dpkg-deb", sources)

        spec_text = render(
            (copr_dir / "hydex-desktop.spec.in").read_text(),
            {
                "@RPM_VERSION@": rpm_version,
                "@RPM_RELEASE@": rpm_release,
                "@PACKAGE_LICENSE@": package_license,
                "@SOURCE_ARCHIVE@": source_archive,
                "@SOURCE_ROOT@": source_root,
                "@UPSTREAM_DEB@": upstream_deb,
                "@HYDEX_ARCHIVE@": hydex_archive,
            },
        )
        spec_path = topdir / "SPECS/hydex-desktop.spec"
        spec_path.write_text(spec_text)
        run(
            [
                "rpmbuild",
                "--define",
                f"_topdir {topdir}",
                "--define",
                f"_tmppath {topdir / 'tmp'}",
                "--define",
                "_rpmformat 4",
                "--define",
                "_source_payload w9.gzdio",
                "-bs",
                str(spec_path),
            ],
            repo,
        )
        packages = list((topdir / "SRPMS").glob("*.src.rpm"))
        if len(packages) != 1:
            raise SystemExit(f"expected one source RPM, found {packages}")
        shutil.copy2(packages[0], outdir / packages[0].name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
