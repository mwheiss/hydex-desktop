import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("build_hydex_release.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("build_hydex_release", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BuildHydexReleaseTests(unittest.TestCase):
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
