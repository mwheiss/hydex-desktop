#!/usr/bin/env python3
"""Promote a successful package to the single alias in the repository's dist root."""

import argparse
import os
import re
import tempfile
from pathlib import Path


def promote_package(
    repo: Path, package: Path, *, package_name: str = "hydex-desktop"
) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9+_.-]*", package_name):
        raise SystemExit(f"invalid package name: {package_name}")
    package = package.resolve(strict=True)
    if (
        not package.is_file()
        or not package.name.startswith(f"{package_name}-")
        or not package.name.endswith((".pkg.tar.zst", ".pkg.tar.xz"))
        or "-latest." in package.name
    ):
        raise SystemExit(f"unexpected package artifact: {package}")

    dist = repo.resolve() / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    latest = dist / f"{package_name}-latest.pkg.tar.zst"
    if (latest.exists() or latest.is_symlink()) and not latest.is_symlink():
        raise SystemExit(f"refusing to overwrite a regular latest file: {latest}")
    relative = Path(os.path.relpath(package, dist))
    if not latest.is_symlink() or latest.readlink() != relative:
        with tempfile.TemporaryDirectory(prefix=".latest-", dir=dist) as temporary:
            staged = Path(temporary) / latest.name
            staged.symlink_to(relative)
            staged.replace(latest)

    for release in dist.glob("release-*"):
        if release.is_dir() and not release.is_symlink():
            legacy = release / latest.name
            if legacy.is_symlink():
                legacy.unlink()
    return latest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--package-name", default="hydex-desktop")
    args = parser.parse_args()
    latest = promote_package(args.repo, args.package, package_name=args.package_name)
    print(f"latest={latest}")
    print(f"target={latest.readlink()}")


if __name__ == "__main__":
    main()
