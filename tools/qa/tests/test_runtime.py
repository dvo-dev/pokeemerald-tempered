import hashlib
from importlib.resources import files
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.qa.qa import runtime
from tools.qa.qa.emu import NativeFile, Session
from tools.qa.qa.errors import QAError
from tools.qa.qa.runtime import package_hash


class RuntimeTests(unittest.TestCase):
    def test_runtime_uses_packaged_patch_and_checks_its_hash(self):
        provenance = files("tools.qa.qa").joinpath("mgba-python.patch").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "mgba"
            package.mkdir()
            for name in ("__init__.py", "core.py", "gba.py", "image.py", "vfs.py"):
                (package / name).write_text("# fixture\n")
            library = root / "libmgba.so"
            library.write_bytes(b"fixture library")
            extension = package / "_pylib.abi3.so"
            extension.write_bytes(b"fixture extension")
            metadata = {
                "schema_version": 1, "status": "built-static-checked",
                "environment": "ad-hoc",
                "mgba_pin": provenance.decode().splitlines()[0].split()[-1],
                "patch_sha256": hashlib.sha256(provenance).hexdigest(),
                "library_path": str(library),
                "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
                "pylib_sha256": hashlib.sha256(extension.read_bytes()).hexdigest(),
                "package_sha256": package_hash(package),
                "python_packages": {"cffi": "fixture"},
            }
            manifest = package / "_qa_runtime.json"
            manifest.write_text(json.dumps(metadata))
            with patch.object(runtime.sysconfig, "get_path", return_value=str(root)):
                with patch.object(runtime, "version", return_value="fixture"):
                    result = runtime.load_runtime()
                    self.assertEqual(result.library, library)
                    self.assertEqual(result.identity["patch_sha256"], metadata["patch_sha256"])
                    metadata["patch_sha256"] = "0" * 64
                    manifest.write_text(json.dumps(metadata))
                    with self.assertRaisesRegex(QAError, "source/patch differs"):
                        runtime.load_runtime()

    def test_package_sources_are_required_and_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            for name in ("__init__.py", "core.py", "gba.py", "image.py", "vfs.py"):
                (package / name).write_text("# source\n")
            original = package_hash(package)
            (package / "core.py").write_text("# changed source\n")
            self.assertNotEqual(package_hash(package), original)
            (package / "vfs.py").unlink()
            with self.assertRaisesRegex(QAError, "incomplete"):
                package_hash(package)

    def test_closed_session_cannot_enter_native_api(self):
        session = object.__new__(Session)
        session.closed = True
        session.core = None
        with self.assertRaises(QAError):
            session.flash()
        with self.assertRaises(QAError):
            session.restore(Path("not-read.state"))
        with self.assertRaises(QAError):
            NativeFile(session)


if __name__ == "__main__":
    unittest.main()
