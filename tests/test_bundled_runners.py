import copy
import hashlib
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

_TEST_ROOT = tempfile.TemporaryDirectory()
os.environ["HOME"] = _TEST_ROOT.name
os.environ["XDG_CONFIG_HOME"] = str(Path(_TEST_ROOT.name) / "config")
os.environ["XDG_DATA_HOME"] = str(Path(_TEST_ROOT.name) / "data")
os.environ["XDG_STATE_HOME"] = str(Path(_TEST_ROOT.name) / "state")

from faugus import bundled_runners

ROOT = Path(__file__).resolve().parents[1]


class BundledRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner_dir = ROOT / "runners"
        cls.manifest = json.loads((cls.runner_dir / "manifest.json").read_text())

    def test_release_manifest_declares_cachyos_default(self):
        self.assertEqual(self.manifest["default"], "cachyos_11.0_20260702-LinUwUx")
        self.assertEqual(self.manifest["release"]["tag"], "uwu-runners-20260702")
        self.assertEqual(
            self.manifest["release"]["base_url"],
            "https://github.com/xXJSONDeruloXx/faugus-launcher/releases/download/uwu-runners-20260702",
        )
        self.assertEqual(
            {runner["root"] for runner in self.manifest["runners"]},
            {"cachyos_11.0_20260702-LinUwUx", "GE-Proton11-1-LinUwUx"},
        )

    def test_large_archives_are_release_assets_not_git_files(self):
        for runner in self.manifest["runners"]:
            self.assertFalse((self.runner_dir / runner["filename"]).exists())
            self.assertEqual(len(runner["sha256"]), 64)
            self.assertGreater(runner["size"], 300_000_000)

    def test_archive_member_validation_rejects_traversal(self):
        safe = tarfile.TarInfo("runner/files/bin/wine")
        bundled_runners.validate_member(safe, "runner")
        for name in ("../escape", "/absolute", "other/files/bin/wine"):
            with self.subTest(name=name), self.assertRaises(bundled_runners.RunnerDownloadError):
                bundled_runners.validate_member(tarfile.TarInfo(name), "runner")

    def _fixture(self, directory):
        directory = Path(directory)
        assets = directory / "assets"
        source = directory / "source"
        installs = directory / "installs"
        name = "Test-LinUwUx"
        root = source / name
        root.mkdir(parents=True)
        proton = root / "proton"
        proton.write_text("#!/bin/sh\nexit 0\n")
        proton.chmod(0o755)
        (root / "compatibilitytool.vdf").write_text('"compatibilitytools" {}\n')
        assets.mkdir()
        archive = assets / f"{name}.tar.xz"
        with tarfile.open(archive, "w:xz") as bundle:
            bundle.add(root, arcname=name)
        payload = archive.read_bytes()
        manifest = {
            "version": 2,
            "default": name,
            "release": {
                "repository": "example/test",
                "tag": "test-assets",
                "base_url": assets.as_uri(),
            },
            "runners": [{
                "filename": archive.name,
                "root": name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }],
        }
        return name, archive, installs, manifest

    def test_release_asset_is_verified_extracted_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            name, archive, installs, manifest = self._fixture(directory)
            progress = []
            target = bundled_runners.ensure_release_runner(
                name,
                progress=lambda downloaded, total: progress.append((downloaded, total)),
                manifest=manifest,
                compatibility_dir=installs,
            )
            self.assertEqual(target, installs / name)
            self.assertTrue(os.access(target / "proton", os.X_OK))
            marker = json.loads((target / bundled_runners.MARKER_NAME).read_text())
            self.assertEqual(marker["sha256"], manifest["runners"][0]["sha256"])
            self.assertEqual(progress[-1][0], manifest["runners"][0]["size"])

            archive.unlink()
            self.assertEqual(
                bundled_runners.ensure_release_runner(
                    name, manifest=manifest, compatibility_dir=installs
                ),
                target,
            )

    def test_bad_release_asset_is_rejected_and_cleaned_up(self):
        with tempfile.TemporaryDirectory() as directory:
            name, _archive, installs, manifest = self._fixture(directory)
            invalid = copy.deepcopy(manifest)
            invalid["runners"][0]["sha256"] = "0" * 64
            with self.assertRaises(bundled_runners.RunnerDownloadError):
                bundled_runners.ensure_release_runner(
                    name, manifest=invalid, compatibility_dir=installs
                )
            self.assertFalse((installs / name).exists())
            self.assertFalse(any(installs.glob("*.part")))

    def test_meson_installs_downloader_and_manifest(self):
        meson = (ROOT / "meson.build").read_text()
        module_meson = (ROOT / "faugus/meson.build").read_text()
        self.assertIn("runner-manifest.json", meson)
        self.assertIn("bundled_runners.py", module_meson)


if __name__ == "__main__":
    unittest.main()
