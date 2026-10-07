import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from update_latest_package import promote_package


class UpdateLatestPackageTests(unittest.TestCase):
    def test_promotes_to_root_and_removes_only_legacy_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            old = repo / "dist/release-old"
            current = repo / "dist/release-current"
            old.mkdir(parents=True)
            current.mkdir()
            package = current / "hydex-desktop-current-1-x86_64.pkg.tar.zst"
            package.write_bytes(b"validated package")
            name = "hydex-desktop-latest.pkg.tar.zst"
            (old / name).symlink_to("missing-old-package.pkg.tar.zst")
            (current / name).symlink_to(package.name)
            unrelated = old / "other-latest.pkg.tar.zst"
            unrelated.write_bytes(b"unrelated")

            latest = promote_package(repo, package)

            self.assertEqual(latest, repo / "dist" / name)
            self.assertEqual(latest.readlink(), Path("release-current") / package.name)
            self.assertEqual(latest.read_bytes(), b"validated package")
            self.assertEqual(list((repo / "dist").glob("release-*/*latest*")), [unrelated])
            self.assertEqual(package.read_bytes(), b"validated package")
            self.assertEqual(promote_package(repo, package), latest)

    def test_missing_artifact_keeps_previous_latest(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            dist = repo / "dist"
            dist.mkdir()
            latest = dist / "hydex-desktop-latest.pkg.tar.zst"
            latest.symlink_to("previous.pkg.tar.zst")
            with self.assertRaises(FileNotFoundError):
                promote_package(repo, dist / "hydex-desktop-missing.pkg.tar.zst")
            self.assertEqual(latest.readlink(), Path("previous.pkg.tar.zst"))

    def test_failed_atomic_promotion_keeps_previous_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            release = repo / "dist/release-current"
            release.mkdir(parents=True)
            package = release / "hydex-desktop-current.pkg.tar.zst"
            package.write_bytes(b"package")
            latest = repo / "dist/hydex-desktop-latest.pkg.tar.zst"
            latest.symlink_to("previous.pkg.tar.zst")
            legacy = release / latest.name
            legacy.symlink_to(package.name)
            with patch.object(Path, "replace", side_effect=OSError("promotion failed")):
                with self.assertRaises(OSError):
                    promote_package(repo, package)
            self.assertEqual(latest.readlink(), Path("previous.pkg.tar.zst"))
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(list((repo / "dist").glob(".latest-*")), [])

    def test_does_not_overwrite_regular_latest_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            dist = repo / "dist"
            dist.mkdir()
            package = dist / "hydex-desktop-current.pkg.tar.zst"
            package.write_bytes(b"package")
            latest = dist / "hydex-desktop-latest.pkg.tar.zst"
            latest.write_bytes(b"existing file")
            with self.assertRaises(SystemExit):
                promote_package(repo, package)
            self.assertEqual(latest.read_bytes(), b"existing file")


if __name__ == "__main__":
    unittest.main()
