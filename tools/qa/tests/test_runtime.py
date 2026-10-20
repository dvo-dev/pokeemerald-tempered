from pathlib import Path
import tempfile
import unittest

from tools.qa.qa.emu import NativeFile, Session
from tools.qa.qa.errors import QAError
from tools.qa.qa.runtime import package_hash


class RuntimeTests(unittest.TestCase):
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
