#!/usr/bin/python3

import builtins
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


w = load("hydex_remote_trust_watcher", "watcher.py")
m = load("hydex_remote_trust_manage", "manage.py")


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.codex = self.home / ".codex"
        self.projectless = self.home / "Documents/Codex"
        self.worktrees = self.codex / "worktrees"
        self.home.mkdir()
        self.codex.mkdir(mode=0o700)
        self.projectless.mkdir(parents=True)
        self.worktrees.mkdir()
        self.config = self.codex / "config.toml"
        self.state = self.home / ".local/state/hydex-desktop/remote-trust-race-workaround.json"
        self.paths = w.Paths(self.home, self.codex, self.config, self.state, self.projectless, self.worktrees)

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, text):
        self.config.write_text(text)
        self.config.chmod(0o600)

    def test_manifest_is_tracked_default_and_requires_remote(self):
        manifest = json.loads((HERE / "feature.json").read_text())
        self.assertTrue(manifest["defaultEnabled"])
        self.assertEqual(manifest["requires"], ["remote-mobile-control"])
        self.assertEqual(manifest["id"], "remote-trust-race-workaround")
        resources = {item["source"]: item for item in manifest["resources"]}
        for relative_path in (
            "tomli/__init__.py",
            "tomli/_parser.py",
            "tomli/_re.py",
            "tomli/_types.py",
            "tomli/LICENSE",
        ):
            self.assertEqual(resources[relative_path]["mode"], "0644")
            self.assertTrue((HERE / relative_path).is_file())

    def test_bundled_tomli_is_the_pre_311_fallback(self):
        original_import = builtins.__import__

        def import_without_tomllib(name, *args, **kwargs):
            if name == "tomllib":
                raise ImportError("forced pre-3.11 fallback")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=import_without_tomllib):
            fallback = load("hydex_remote_trust_watcher_fallback", "watcher.py")
        self.assertEqual(fallback.tomllib.__version__, "1.2.3")
        self.assertEqual(
            fallback.tomllib.loads('model = "gpt-5.6-sol"'),
            {"model": "gpt-5.6-sol"},
        )

    def test_projectless_requires_explicit_root_trust_and_date_direct_child(self):
        candidate = self.projectless / f"{time.strftime('%Y-%m-%d')}-test-2"
        candidate.mkdir()
        self.write_config("")
        self.assertFalse(w.projectless_candidate(self.paths, candidate, w.read_config(self.config)))
        self.write_config(f'[projects."{self.projectless}"]\ntrust_level = "trusted"\n')
        cfg = w.read_config(self.config)
        self.assertTrue(w.projectless_candidate(self.paths, candidate, cfg))
        nested = candidate / "nested"
        nested.mkdir()
        self.assertFalse(w.projectless_candidate(self.paths, nested, cfg))
        bad = self.projectless / "not-date-prefixed"
        bad.mkdir()
        self.assertFalse(w.projectless_candidate(self.paths, bad, cfg))

    def test_projectless_never_overrides_explicit_untrusted(self):
        candidate = self.projectless / f"{time.strftime('%Y-%m-%d')}-blocked"
        candidate.mkdir()
        self.write_config(
            f'[projects."{self.projectless}"]\ntrust_level = "trusted"\n\n'
            f'[projects."{candidate}"]\ntrust_level = "untrusted"\n'
        )
        cfg = w.read_config(self.config)
        self.assertFalse(w.projectless_candidate(self.paths, candidate, cfg))
        self.assertFalse(w.add_exact_trust(self.config, candidate))
        self.assertIn('trust_level = "untrusted"', self.config.read_text())

    def test_worktree_requires_name_of_existing_explicitly_trusted_git_checkout(self):
        checkout = self.home / "src/example-repo"
        checkout.mkdir(parents=True)
        (checkout / ".git").mkdir()
        generated = self.worktrees / "a1b2c3"
        generated.mkdir()
        candidate = generated / "example-repo"
        candidate.mkdir()
        self.write_config(f'[projects."{checkout}"]\ntrust_level = "trusted"\n')
        cfg = w.read_config(self.config)
        self.assertTrue(w.worktree_candidate(self.paths, candidate, cfg))
        other = generated / "other-repo"
        other.mkdir()
        self.assertFalse(w.worktree_candidate(self.paths, other, cfg))

    def test_exact_trust_append_preserves_existing_config(self):
        candidate = self.projectless / f"{time.strftime('%Y-%m-%d')}-new-chat"
        candidate.mkdir()
        original = f'model = "gpt-5.6-sol"\n\n[projects."{self.projectless}"]\ntrust_level = "trusted"\n'
        self.write_config(original)
        self.assertTrue(w.add_exact_trust(self.config, candidate))
        text = self.config.read_text()
        self.assertTrue(text.startswith(original))
        self.assertTrue(w.explicitly_trusted(w.read_config(self.config), candidate))

    def test_exact_trust_does_not_append_across_atomic_untrusted_replacement(self):
        candidate = self.projectless / f"{time.strftime('%Y-%m-%d')}-blocked-race"
        candidate.mkdir()
        root_only = f'[projects."{self.projectless}"]\ntrust_level = "trusted"\n'
        self.write_config(root_only)
        replacement = self.config.with_name("config.toml.new")
        original_load = w.tomllib.load

        def replace_then_load(stream):
            replacement.write_text(
                root_only
                + f'\n[projects."{candidate}"]\ntrust_level = "untrusted"\n'
            )
            replacement.chmod(0o600)
            os.replace(replacement, self.config)
            return original_load(stream)

        with patch.object(w.tomllib, "load", side_effect=replace_then_load):
            self.assertFalse(w.add_exact_trust(self.config, candidate))
        config = w.read_config(self.config)
        self.assertTrue(w.explicitly_untrusted(config, candidate))
        candidate_table = self.config.read_text().split(str(candidate), 1)[1]
        self.assertNotIn('trust_level = "trusted"\n', candidate_table)

    def test_inotify_observes_new_projectless_child_and_can_win_race(self):
        self.write_config(f'[projects."{self.projectless}"]\ntrust_level = "trusted"\n')
        ino = w.Inotify()
        ino.add(self.projectless)
        candidate = self.projectless / f"{time.strftime('%Y-%m-%d')}-race"
        candidate.mkdir()
        seen = False
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not seen:
            for base, name, mask in ino.events(0.1) or ():
                if base == self.projectless and name == candidate.name and mask & w.IN_ISDIR:
                    w.handle_candidate(self.paths, candidate)
                    seen = True
                    break
        self.assertTrue(seen)
        self.assertTrue(w.explicitly_trusted(w.read_config(self.config), candidate))

    def test_reconcile_restores_entry_after_atomic_config_replacement(self):
        candidate = self.projectless / f"{time.strftime('%Y-%m-%d')}-restore"
        candidate.mkdir()
        root_only = f'[projects."{self.projectless}"]\ntrust_level = "trusted"\n'
        self.write_config(root_only)
        ino = w.Inotify()
        self.assertTrue(w.remember_and_trust(self.paths, candidate))
        w.reconcile(self.paths, ino)
        self.assertTrue(w.explicitly_trusted(w.read_config(self.config), candidate))
        replacement = self.config.with_name("config.toml.new")
        replacement.write_text(root_only)
        replacement.chmod(0o600)
        os.replace(replacement, self.config)
        w.reconcile(self.paths, ino)
        self.assertTrue(w.explicitly_trusted(w.read_config(self.config), candidate))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.app = self.root / "opt/hydex-desktop"
        feature = self.app / ".codex-linux/features/remote-trust-race-workaround"
        feature.mkdir(parents=True)
        watcher = feature / "watcher.py"
        shutil.copyfile(HERE / "watcher.py", watcher)
        watcher.chmod(0o755)
        self.env = patch.dict(os.environ, {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
        })
        self.env.start()
        self.home.mkdir(parents=True)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_ensure_writes_owned_unit_and_enables_it(self):
        calls = []
        def fake_run(args, check=True):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch.object(m, "run", fake_run), patch.object(m.os, "getuid", return_value=1000):
            m.ensure(str(self.app))
        unit = m.unit_path()
        text = unit.read_text()
        self.assertTrue(text.startswith(m.MARKER))
        self.assertIn(m.quote_unit(m.sys.executable, argument=True), text)
        self.assertIn(str(self.app / ".codex-linux/features/remote-trust-race-workaround/watcher.py"), text)
        self.assertIn(["systemctl", "--user", "daemon-reload"], calls)
        self.assertIn(["systemctl", "--user", "enable", "--now", m.UNIT], calls)


if __name__ == "__main__":
    unittest.main()
