import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.qa.qa import checkpoint
from tools.qa.qa.errors import QAError, UsageError


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bundle_id = "a" * 64
        self.runtime = {"mgba_pin": "pin", "library_sha256": "native",
                        "pylib_sha256": "binding"}
        self.options = {
            "bundle_id": self.bundle_id, "runtime": self.runtime,
            "frame": 600, "epoch_ms": 1767268800000,
            "core_state": b"native-state", "savedata": b"\xff" * checkpoint.SAVE_BYTES,
            "observations": {"flash_bank": 0x10000},
        }

    def write(self, name="test.state", **options):
        values = dict(self.options, **options)
        return checkpoint.write_checkpoint(self.root / name, **values)

    def read(self, name="test.state", **options):
        return checkpoint.read_checkpoint(
            self.root / name, **dict(bundle_id=self.bundle_id, runtime=self.runtime, **options)
        )

    def rewrite(self, change):
        path = self.root / "test.state"
        with zipfile.ZipFile(path) as archive:
            values = {name: archive.read(name) for name in archive.namelist()}
        change(values)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, value in values.items():
                archive.writestr(name, value)

    def mutate_manifest(self, change):
        def update(values):
            manifest = json.loads(values["manifest.json"])
            change(manifest)
            values["manifest.json"] = json.dumps(manifest).encode()
        self.rewrite(update)

    def test_roundtrip_and_component_hashes(self):
        written = self.write()
        loaded = self.read()
        self.assertEqual(loaded, written)
        self.assertEqual(loaded.savedata, self.options["savedata"])
        self.assertEqual(loaded.core_state, b"native-state")
        with zipfile.ZipFile(self.root / "test.state") as archive:
            self.assertEqual(set(archive.namelist()), checkpoint.COMPONENTS)
            self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0)
                                for item in archive.infolist()))

    def test_identical_inputs_produce_identical_archive_bytes(self):
        first = self.write("a.state")
        second = self.write("b.state")
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual((self.root / "a.state").read_bytes(),
                         (self.root / "b.state").read_bytes())

    def test_existing_output_is_never_overwritten(self):
        original = self.write()
        with self.assertRaises(FileExistsError):
            self.write(core_state=b"different")
        self.assertEqual(self.read().sha256, original.sha256)

    def test_bundle_mismatch_is_rejected_before_native_load(self):
        self.write()
        with self.assertRaisesRegex(QAError, "different build bundle"):
            checkpoint.read_checkpoint(
                self.root / "test.state", bundle_id="b" * 64, runtime=self.runtime
            )

    def test_runtime_mismatch_is_rejected_before_native_load(self):
        self.write()
        with self.assertRaisesRegex(QAError, "different emulator"):
            checkpoint.read_checkpoint(
                self.root / "test.state", bundle_id=self.bundle_id,
                runtime={"mgba_pin": "other"},
            )

    def test_conflicting_epoch_is_usage_error(self):
        self.write()
        with self.assertRaises(UsageError) as raised:
            self.read(epoch_ms=self.options["epoch_ms"] + 1000)
        self.assertEqual(raised.exception.exit_code, 64)

    def test_corrupt_component_fails(self):
        self.write()
        self.rewrite(lambda values: values.update({"core.state": b"altered-state"}))
        with self.assertRaisesRegex(QAError, "integrity mismatch"):
            self.read()

    def test_runtime_identity_tampering_fails(self):
        self.write()
        self.mutate_manifest(lambda value: value.update({"runtime_id": "bad"}))
        with self.assertRaisesRegex(QAError, "runtime identity"):
            self.read()

    def test_pressed_keys_fail(self):
        self.write()
        self.mutate_manifest(lambda value: value.update({"keys": 1}))
        with self.assertRaisesRegex(QAError, "released keys"):
            self.read()

    def test_invalid_schema_or_scalar_types_fail(self):
        for field, invalid in (("schema_version", True), ("frame", True),
                               ("epoch_ms", 0), ("keys", 0.0)):
            with self.subTest(field=field):
                self.write(field + ".state")
                path = self.root / (field + ".state")
                with zipfile.ZipFile(path) as archive:
                    values = {name: archive.read(name) for name in archive.namelist()}
                manifest = json.loads(values["manifest.json"])
                manifest[field] = invalid
                values["manifest.json"] = json.dumps(manifest).encode()
                with zipfile.ZipFile(path, "w") as archive:
                    for name, data in values.items():
                        archive.writestr(name, data)
                with self.assertRaises(QAError):
                    self.read(field + ".state")

    def test_invalid_save_length_and_core_size_fail(self):
        for overrides in ({"savedata": b""}, {"savedata": b"\xff" * (checkpoint.SAVE_BYTES + 16)},
                          {"core_state": b""},
                          {"core_state": b"x" * (checkpoint.MAX_CORE_BYTES + 1)}):
            with self.subTest(overrides=list(overrides)), self.assertRaises(QAError):
                self.write(**overrides)

    def test_invalid_observations_do_not_default_to_success(self):
        with self.assertRaisesRegex(QAError, "observations"):
            self.write(observations=[])

    def test_extra_path_or_member_is_rejected_without_extraction(self):
        self.write()
        escaped = self.root.parent / ("qa-escape-" + self.root.name)
        self.assertFalse(escaped.exists())
        self.rewrite(lambda values: values.update({"../" + escaped.name: b"untrusted"}))
        with self.assertRaisesRegex(QAError, "exactly the three"):
            self.read()
        self.assertFalse(escaped.exists())

    def test_compressed_archive_is_rejected(self):
        self.write()
        path = self.root / "test.state"
        with zipfile.ZipFile(path) as archive:
            values = {name: archive.read(name) for name in archive.namelist()}
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in values.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(QAError, "Invalid or oversized"):
            self.read()

    def test_duplicate_members_are_rejected(self):
        self.write()
        path = self.root / "test.state"
        with zipfile.ZipFile(path, "a") as archive:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("core.state", b"duplicate")
        with self.assertRaisesRegex(QAError, "exactly the three"):
            self.read()

    def test_parent_checkpoint_provenance_is_preserved(self):
        self.write(parent_sha256="c" * 64)
        self.assertEqual(self.read().manifest["parent_sha256"], "c" * 64)

    def test_malformed_archive_is_explicit_error(self):
        (self.root / "test.state").write_bytes(b"not a zip archive")
        with self.assertRaisesRegex(QAError, "Cannot read checkpoint"):
            self.read()


if __name__ == "__main__":
    unittest.main()
