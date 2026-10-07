import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("build_hydex_release.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("build_hydex_release", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BuildHydexReleaseTests(unittest.TestCase):
    def run_release(self, repo, validation, *, validate_only=False):
        output = repo / "dist/release-2026.10.07.194202"
        output.mkdir(parents=True)
        version = "2026.10.07.194202"
        inputs = [repo / name for name in ("upstream.deb", "codex", "features.json")]
        for path in inputs:
            path.write_bytes(b"input")
        inputs[2].write_text(json.dumps({"enabled": sorted(MODULE.REQUIRED_FEATURES)}))
        args = SimpleNamespace(
            repo=repo,
            output_dir=output,
            package_version=version,
            upstream_deb=inputs[0],
            hydex_bin=inputs[1],
            features_config=inputs[2],
            temp_dir=repo / "scratch",
            candidate_dir=repo / "candidate",
            max_build_threads=1,
            report=output / "release-report.json",
            plan_only=False,
            validate_only=validate_only,
        )

        def build(command, cwd, *, env, capture):
            self.assertEqual(env["PACKAGE_LATEST_POLICY"], "defer")
            self.assertEqual(
                (repo / "dist/hydex-desktop-latest.pkg.tar.zst").readlink(),
                Path("previous.pkg.tar.zst"),
            )
            if command == ["make", "pacman"]:
                package = MODULE.expected_artifacts(output, version)["pacman"]
                package.write_bytes(b"new package")

        candidate = {"buildInfo": {"source": {}, "linuxFeatures": {"enabled": []}}}
        with patch.multiple(
            MODULE,
            parse_args=Mock(return_value=args),
            require_commands=Mock(),
            deb_metadata=Mock(return_value={"Version": "upstream-version"}),
            command_version=Mock(return_value="codex-version"),
            sha256=Mock(return_value="digest"),
            git=Mock(return_value=SimpleNamespace(stdout="")),
            build_candidate=Mock(),
            validate_candidate=Mock(return_value=candidate),
            run=Mock(side_effect=build),
            validate_packages=validation,
        ):
            MODULE.main()
        return args

    def test_successful_release_promotes_after_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "dist").mkdir()
            latest = repo / "dist/hydex-desktop-latest.pkg.tar.zst"
            latest.symlink_to("previous.pkg.tar.zst")

            def validate(args):
                self.assertEqual(latest.readlink(), Path("previous.pkg.tar.zst"))
                return {}

            args = self.run_release(repo, Mock(side_effect=validate))

            self.assertEqual(
                latest.resolve(),
                MODULE.expected_artifacts(args.output_dir, args.package_version)["pacman"],
            )
            self.assertTrue(json.loads(args.report.read_text())["validated"])
            self.assertEqual(list((repo / "dist").glob("release-*/*latest*")), [])

    def test_failed_release_validation_keeps_previous_latest(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "dist").mkdir()
            latest = repo / "dist/hydex-desktop-latest.pkg.tar.zst"
            latest.symlink_to("previous.pkg.tar.zst")
            validation = Mock(side_effect=SystemExit("package validation failed"))

            with self.assertRaisesRegex(SystemExit, "package validation failed"):
                self.run_release(repo, validation)

            self.assertEqual(latest.readlink(), Path("previous.pkg.tar.zst"))
            self.assertEqual(list((repo / "dist").glob("release-*/*latest*")), [])

    def test_validation_only_does_not_change_latest(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "dist").mkdir()
            latest = repo / "dist/hydex-desktop-latest.pkg.tar.zst"
            latest.symlink_to("previous.pkg.tar.zst")

            self.run_release(repo, Mock(return_value={}), validate_only=True)

            self.assertEqual(latest.readlink(), Path("previous.pkg.tar.zst"))

    def test_expected_artifact_paths_match_native_builders(self):
        root = Path("/dist")
        version = "2026.09.07.104041"
        self.assertEqual(
            MODULE.expected_artifacts(root, version),
            {
                "pacman": root / "hydex-desktop-2026.09.07.104041-1-x86_64.pkg.tar.zst",
                "fullRpm": root / "hydex-desktop-2026.09.07.104041-1.x86_64.rpm",
                "rhel9Rpm": root / "hydex-desktop-2026.09.07.104041-rhel9.x86_64.rpm",
                "rhel7Rpm": root / "hydex-desktop-2026.09.07.104041-rhel7.x86_64.rpm",
                "rhel7CliRpm": root
                / "hydex-desktop-cli-runtime-2026.09.07.104041-rhel7.x86_64.rpm",
            },
        )

    def test_release_features_must_match_exact_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "features.json"
            config.write_text(
                f"{json.dumps({'enabled': sorted(MODULE.REQUIRED_FEATURES)})}\n"
            )
            MODULE.validate_features(config)

    def test_tracked_release_features_include_remote_trust_workaround(self):
        config = MODULE_PATH.parents[1] / ".copr" / "features.json"
        self.assertIn("remote-trust-race-workaround", MODULE.REQUIRED_FEATURES)
        MODULE.validate_features(config)

    def test_release_features_reject_missing_or_shared_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "features.json"
            config.write_text('{"enabled":["remote-mobile-control","hydex-offload"]}\n')
            with self.assertRaises(SystemExit):
                MODULE.validate_features(config)
            config.write_text(
                '{"enabled":["remote-mobile-control","hydex-offload",'
                '"persistent-app-server","shared-app-server-socket"]}\n'
            )
            with self.assertRaises(SystemExit):
                MODULE.validate_features(config)


if __name__ == "__main__":
    unittest.main()
