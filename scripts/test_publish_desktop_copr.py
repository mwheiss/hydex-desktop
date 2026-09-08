import argparse
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("publish_desktop_copr.py")
SPEC = importlib.util.spec_from_file_location("publish_desktop_copr", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class PublishDesktopCoprTests(unittest.TestCase):
    def test_all_prebuilt_tiers_own_chatgpt_compatibility_surface(self):
        for tier in MODULE.TIERS:
            template = (
                MODULE_PATH.parents[1] / "packaging" / "copr" / tier.template
            ).read_text()
            self.assertRegex(template, r"(?m)^Provides:\s+.*\bchatgpt\b", tier.name)
            self.assertRegex(template, r"(?m)^Conflicts:\s+.*\bchatgpt\b", tier.name)
            for path in (
                "/usr/bin/chatgpt",
                "/usr/share/applications/chatgpt.desktop",
                "/usr/share/icons/hicolor/256x256/apps/chatgpt.png",
            ):
                self.assertIn(path, template, tier.name)

    def test_tiers_cover_each_chroot_once_in_sequential_order(self):
        self.assertEqual([tier.name for tier in MODULE.TIERS], ["rhel9", "rhel7", "full"])
        chroots = [chroot for tier in MODULE.TIERS for chroot in tier.chroots]
        self.assertEqual(
            chroots,
            [
                "rhel-8-x86_64",
                "epel-8-x86_64",
                "rhel-9-x86_64",
                "epel-9-x86_64",
                "rhel-7-x86_64",
                "epel-7-x86_64",
                "rhel-10-x86_64",
                "epel-10-x86_64",
            ],
        )
        self.assertEqual(len(chroots), len(set(chroots)))

    def test_native_names_match_package_builders(self):
        root = Path("/native")
        version = "2026.09.07.104041"
        self.assertEqual(
            MODULE.native_paths(root, MODULE.TIERS[0], version),
            (root / "hydex-desktop-2026.09.07.104041-rhel9.x86_64.rpm",),
        )
        self.assertEqual(
            MODULE.native_paths(root, MODULE.TIERS[1], version),
            (
                root / "hydex-desktop-cli-runtime-2026.09.07.104041-rhel7.x86_64.rpm",
                root / "hydex-desktop-2026.09.07.104041-rhel7.x86_64.rpm",
            ),
        )
        self.assertEqual(
            MODULE.native_paths(root, MODULE.TIERS[2], version),
            (root / "hydex-desktop-2026.09.07.104041-1.x86_64.rpm",),
        )

    def test_templates_render_expected_immutable_sources(self):
        repo = MODULE_PATH.resolve().parents[1]
        version = "2026.09.07.104041"
        expected = {
            "rhel9": f"Source0:        hydex-desktop-{version}-rhel9-x86_64-payload.tar.zst",
            "rhel7": f"Source0:        hydex-desktop-{version}-rhel7-main-payload.tar.gz",
            "full": f"Source0:        hydex-desktop-{version}-1-x86_64-payload.tar.zst",
        }
        with tempfile.TemporaryDirectory() as temporary:
            for tier in MODULE.TIERS:
                output = Path(temporary) / f"{tier.name}.spec"
                MODULE.render_spec(repo / "packaging" / "copr" / tier.template, output, version)
                rendered = output.read_text()
                self.assertNotIn("@VERSION@", rendered)
                self.assertNotIn("@CHANGELOG_DATE@", rendered)
                self.assertIn(expected[tier.name], rendered)

    def test_normalized_manifest_ignores_build_ids_and_directory_sizes(self):
        rows = [
            ("/usr/lib/.build-id/aa/link", "120777", "12", "", "../../binary"),
            ("/opt/hydex-desktop", "40755", "47", "", ""),
            ("/opt/hydex-desktop/resources/codex", "100755", "99", "digest", ""),
        ]
        with mock.patch.object(MODULE, "rpm_file_rows", return_value=rows):
            self.assertEqual(
                MODULE.normalized_manifest(Path("package.rpm")),
                [
                    ("/opt/hydex-desktop", "40755", "0", "", ""),
                    ("/opt/hydex-desktop/resources/codex", "100755", "99", "digest", ""),
                ],
            )

    def test_normalized_provides_ignores_each_package_own_version(self):
        package = Path("hydex-desktop-cli-runtime.rpm")
        with (
            mock.patch.object(
                MODULE,
                "rpm_identity",
                return_value={"name": "hydex-desktop-cli-runtime"},
            ),
            mock.patch.object(
                MODULE,
                "rpm_lines",
                return_value=[
                    "hydex-desktop-cli-runtime = 1-rhel7.el7_9",
                    "hydex-desktop-cli-runtime(x86-64) = 1-rhel7.el7_9",
                    "shared-provider = 1",
                ],
            ),
        ):
            self.assertEqual(MODULE.normalized_provides(package), ["shared-provider = 1"])

    def test_parse_build_id_accepts_copr_output(self):
        output = "Build was added\nhttps://copr.fedorainfracloud.org/coprs/build/10956971\nCreated builds: 10956971\n"
        self.assertEqual(MODULE.parse_build_id(output), 10956971)

    def test_publish_waits_for_each_tier_before_submitting_next(self):
        events = []
        build_ids = iter((101, 102, 103))

        def runner(command, cwd, *, capture=True, check=True, input_bytes=None):
            del cwd, capture, check, input_bytes
            if command[1] == "build":
                build_id = next(build_ids)
                events.append(("build", command[3]))
                return subprocess.CompletedProcess(command, 0, f"Created builds: {build_id}\n", "")
            if command[1] == "watch-build":
                events.append(("watch", int(command[2])))
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(command)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = argparse.Namespace(repo=root, output_dir=root, project="mheiss/hydex")
            report = {
                "tiers": {
                    tier.name: {"srpm": {"path": f"/{tier.name}.src.rpm"}}
                    for tier in MODULE.TIERS
                }
            }
            with mock.patch.object(MODULE, "copr_status", side_effect=["succeeded"] * 3):
                MODULE.publish_tiers(args, report, runner=runner)

        self.assertEqual(
            events,
            [
                ("build", "/rhel9.src.rpm"),
                ("watch", 101),
                ("build", "/rhel7.src.rpm"),
                ("watch", 102),
                ("build", "/full.src.rpm"),
                ("watch", 103),
            ],
        )

    def test_rhel7_live_comparison_accepts_already_omitted_recommendations(self):
        native = Path("native.rpm")
        rebuilt = Path("rebuilt.rpm")
        live = Path("live.rpm")

        def rpm_identity(path):
            del path
            return {"name": MODULE.PACKAGE}

        def rpm_lines(path, option):
            if option != "--recommends":
                return []
            return {
                native: ["kdialog", "zenity"],
                rebuilt: [],
                live: [],
            }[path]

        with (
            mock.patch.object(MODULE, "rpm_identity", side_effect=rpm_identity),
            mock.patch.object(MODULE, "normalized_manifest", return_value=[]),
            mock.patch.object(MODULE, "normalized_requires", return_value=[]),
            mock.patch.object(MODULE, "normalized_provides", return_value=[]),
            mock.patch.object(MODULE, "rpm_lines", side_effect=rpm_lines),
            mock.patch.object(MODULE, "rpm_query", return_value=""),
        ):
            MODULE.compare_package_sets((native,), (rebuilt,), MODULE.TIERS[1])
            MODULE.compare_package_sets(
                (rebuilt,),
                (live,),
                MODULE.TIERS[1],
                source_is_compat_rebuild=True,
            )

    def test_validated_record_files_reuses_complete_matching_readback(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "package.rpm"
            package.write_bytes(b"rpm")
            identity = {"name": "hydex-desktop"}
            record = [{"path": str(package), "sha256": "digest", "identity": identity}]
            with (
                mock.patch.object(MODULE, "sha256", return_value="digest"),
                mock.patch.object(MODULE, "rpm_identity", return_value=identity),
            ):
                self.assertEqual(MODULE.validated_record_files(record, 1), [package])
                self.assertIsNone(MODULE.validated_record_files(record, 2))

    def test_resume_download_reuses_existing_file_before_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "package.rpm"
            destination.write_bytes(b"complete")
            with mock.patch.object(MODULE, "run") as run:
                MODULE.download_file(
                    "https://example.invalid/package.rpm",
                    destination,
                    Path(temporary),
                    reuse_existing=True,
                )
            run.assert_not_called()

    def test_cleanup_keeps_report_specs_srpms_and_readback(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for name in ("sources", "rpmbuild-rhel9", "rebuild-full"):
                path = output / name
                path.mkdir()
                (path / "artifact").write_text("generated")
            for name in ("specs", "srpms", "readback"):
                path = output / name
                path.mkdir()
                (path / "artifact").write_text("retained")
            report = {}
            MODULE.cleanup_intermediates(output, report)
            self.assertEqual(report["intermediatesRetained"], False)
            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                ["readback", "report.json", "specs", "srpms"],
            )


if __name__ == "__main__":
    unittest.main()
