import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("build-source-rpm.py")
SPEC = importlib.util.spec_from_file_location("build_source_rpm", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SourceRpmTests(unittest.TestCase):
    def test_render_replaces_every_placeholder(self) -> None:
        template = "@RPM_VERSION@ @RPM_RELEASE@ @PACKAGE_LICENSE@ @SOURCE_ARCHIVE@ @SOURCE_ROOT@ @UPSTREAM_DEB@ @HYDEX_ARCHIVE@"
        values = {
            "@RPM_VERSION@": "0.0.0_copr_dummy",
            "@RPM_RELEASE@": "0.1.dummy",
            "@PACKAGE_LICENSE@": "MIT",
            "@SOURCE_ARCHIVE@": "source.tar.gz",
            "@SOURCE_ROOT@": "source",
            "@UPSTREAM_DEB@": "dummy.deb",
            "@HYDEX_ARCHIVE@": "hydex.tar.gz",
        }

        rendered = MODULE.render(template, values)

        self.assertNotIn("@", rendered)
        self.assertEqual(rendered.split(), list(values.values()))


if __name__ == "__main__":
    unittest.main()
