import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path
from unittest.mock import patch

from robotsoul.resources import ResourceError, default_root, download, import_bundle, install, pack, verify


class VoiceResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "resources"
        self.payload = self._archive()
        self.item = {
            "id": "sample", "purpose": "test", "version": "1", "architectures": ["linux-aarch64"],
            "url": "https://example.invalid/sample.tar.bz2", "size_bytes": len(self.payload),
            "sha256": hashlib.sha256(self.payload).hexdigest(), "archive": "tar.bz2",
            "install_dir": "models/sample", "license": "test"
        }
        self.manifest = {"schema_version": 1, "resources": [self.item]}
        (self.root / "downloads").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def _archive(self, name="model/config.txt"):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:bz2") as archive:
            data = b"voice model"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def test_default_resource_root_targets_go2_arm64_even_on_development_host(self):
        self.assertEqual(default_root(), Path("build/linux-aarch64/voice/resources"))

    def test_download_verifies_and_atomically_caches(self):
        with patch("robotsoul.resources.urllib.request.urlopen", return_value=io.BytesIO(self.payload)):
            download(self.manifest, self.root)
        target = self.root / "downloads/sample.tar.bz2"
        self.assertEqual(target.read_bytes(), self.payload)
        target.write_bytes(b"preserve this")
        with patch("robotsoul.resources.urllib.request.urlopen", return_value=io.BytesIO(b"bad")):
            with self.assertRaises(ResourceError):
                download(self.manifest, self.root)
        self.assertEqual(target.read_bytes(), b"preserve this")

    def test_install_and_verify(self):
        (self.root / "downloads/sample.tar.bz2").write_bytes(self.payload)
        install(self.manifest, self.root)
        installed_file = self.root / "models/sample/model/config.txt"
        self.assertEqual(installed_file.read_bytes(), b"voice model")
        verify(self.manifest, self.root)
        installed_file.write_bytes(b"modified model")
        with self.assertRaisesRegex(ResourceError, "file mismatch"):
            verify(self.manifest, self.root)

    def test_install_validates_all_archives_before_replacing_any_resource(self):
        first = dict(self.item, id="sample-first", install_dir="models/sample-first")
        second = dict(self.item, id="sample-second", install_dir="models/sample-second")
        manifest = {"schema_version": 1, "resources": [first, second]}
        (self.root / "downloads/sample-first.tar.bz2").write_bytes(self.payload)
        first_target = self.root / "models/sample-first"
        first_target.mkdir(parents=True)
        (first_target / "keep.txt").write_text("keep")
        with self.assertRaisesRegex(ResourceError, "Missing archive"):
            install(manifest, self.root)
        self.assertEqual((first_target / "keep.txt").read_text(), "keep")

    def test_file_resource_installs_named_target_and_verify_checks_its_hash(self):
        payload = b"onnx model"
        item = dict(self.item, archive="file", filename="silero_vad.int8.onnx", install_dir="models/vad")
        item["size_bytes"] = len(payload)
        item["sha256"] = hashlib.sha256(payload).hexdigest()
        manifest = {"schema_version": 1, "resources": [item]}
        (self.root / "downloads/sample.bin").write_bytes(payload)
        install(manifest, self.root)
        target = self.root / "models/vad/silero_vad.int8.onnx"
        self.assertEqual(target.read_bytes(), payload)
        verify(manifest, self.root)
        target.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ResourceError, "mismatch"):
            verify(manifest, self.root)

    def test_path_traversal_archive_is_rejected_without_replacing_target(self):
        target = self.root / "models/sample"
        target.mkdir(parents=True)
        (target / "existing").write_text("keep")
        malicious = self._archive("../escaped")
        self.item["size_bytes"] = len(malicious)
        self.item["sha256"] = hashlib.sha256(malicious).hexdigest()
        (self.root / "downloads/sample.tar.bz2").write_bytes(malicious)
        with self.assertRaises(ResourceError):
            install(self.manifest, self.root)
        self.assertEqual((target / "existing").read_text(), "keep")
        self.assertFalse((self.root / "models/escaped").exists())

    def test_install_rejects_symlinked_destination_parent(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")
        item = dict(self.item, install_dir="linked/model")
        manifest = {"schema_version": 1, "resources": [item]}
        (self.root / "downloads/sample.tar.bz2").write_bytes(self.payload)
        with self.assertRaisesRegex(ResourceError, "Symbolic link"):
            install(manifest, self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_offline_pack_import_install_verify_roundtrip(self):
        (self.root / "downloads/sample.tar.bz2").write_bytes(self.payload)
        bundle = self.base / "offline.zip"
        pack(self.manifest, self.root, bundle)
        with zipfile.ZipFile(bundle) as archive:
            embedded = json.loads(archive.read("manifest.json"))
        self.assertEqual([item["id"] for item in embedded["resources"]], ["sample"])
        other = self.base / "target"
        import_bundle(bundle, self.manifest, other)
        install(self.manifest, other)
        verify(self.manifest, other)

    def test_offline_import_rejects_corrupt_archive_without_replacing_cache(self):
        bundle = self.base / "bad.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("manifest.json", json.dumps(self.manifest))
            archive.writestr("archives/sample.tar.bz2", b"wrong")
        target = self.root / "downloads/sample.tar.bz2"
        target.write_bytes(b"keep")
        with self.assertRaises(ResourceError):
            import_bundle(bundle, self.manifest, self.root)
        self.assertEqual(target.read_bytes(), b"keep")

    def test_offline_import_rejects_manifest_resource_set_mismatch(self):
        bundle = self.base / "wrong-manifest.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"schema_version": 1, "resources": []}))
            archive.writestr("archives/sample.tar.bz2", self.payload)
        with self.assertRaisesRegex(ResourceError, "does not match"):
            import_bundle(bundle, self.manifest, self.root)

    def test_offline_import_rolls_back_all_targets_if_replacement_fails(self):
        second = dict(self.item, id="sample-second", install_dir="models/sample-second")
        manifest = {"schema_version": 1, "resources": [self.item, second]}
        bundle = self.base / "rollback.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("archives/sample.tar.bz2", self.payload)
            archive.writestr("archives/sample-second.tar.bz2", self.payload)
        first_target = self.root / "downloads/sample.tar.bz2"
        second_target = self.root / "downloads/sample-second.tar.bz2"
        first_target.write_bytes(b"first-old")
        second_target.write_bytes(b"second-old")
        replace = __import__("os").replace

        def fail_final_replace(source, target):
            if Path(target) == second_target and Path(source).name.startswith(".import-"):
                raise OSError("simulated disk failure")
            return replace(source, target)

        with patch("robotsoul.resources.os.replace", side_effect=fail_final_replace):
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                import_bundle(bundle, manifest, self.root)
        self.assertEqual(first_target.read_bytes(), b"first-old")
        self.assertEqual(second_target.read_bytes(), b"second-old")


if __name__ == "__main__":
    unittest.main()
