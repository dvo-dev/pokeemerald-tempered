import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RuntimeBuildDestinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.build = self.root / "build"
        self.prefix = self.root / "prefix"
        self.venv = self.root / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(self.venv)],
            check=True, capture_output=True, timeout=30,
        )
        self.python = self.venv / "bin/python"
        purelib = subprocess.check_output(
            [str(self.python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            text=True,
        ).strip()
        self.package = Path(purelib) / "mgba"
        self.script = Path(__file__).resolve().parents[1] / "build_runtime.sh"

    def run_script(self):
        return subprocess.run(
            ["bash", str(self.script), str(self.source), str(self.build),
             str(self.prefix), str(self.python)],
            cwd=self.root, capture_output=True, text=True, timeout=30,
        )

    def test_reused_prefix_is_rejected_without_changing_existing_bytes(self):
        (self.prefix / "share/qa").mkdir(parents=True)
        (self.prefix / "share/qa/runtime.json").write_text(json.dumps({"existing": True}))
        (self.prefix / ".library").write_bytes(b"existing native bytes")
        before = {p.relative_to(self.prefix): p.read_bytes()
                  for p in self.prefix.rglob("*") if p.is_file()}
        result = self.run_script()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("prefix must be absent or empty", result.stderr)
        self.assertEqual(
            {p.relative_to(self.prefix): p.read_bytes()
             for p in self.prefix.rglob("*") if p.is_file()}, before
        )
        self.assertFalse(self.build.exists())

    def test_existing_venv_package_is_rejected_before_creating_prefix(self):
        self.package.mkdir()
        marker = self.package / "__init__.py"
        marker.write_text("raise RuntimeError('must not be imported during preflight')\n")
        before = marker.read_bytes()
        result = self.run_script()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("existing mGBA Python package", result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertEqual(marker.read_bytes(), before)
        self.assertFalse(self.build.exists())

    def test_broken_package_symlink_is_also_rejected(self):
        self.package.symlink_to(self.root / "missing")
        result = self.run_script()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("existing mGBA Python package", result.stderr)
        self.assertTrue(self.package.is_symlink())
        self.assertFalse(self.prefix.exists())

    def test_fresh_destinations_reach_source_validation_without_writes(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("mGBA source is not at the recorded pin", result.stderr)
        self.assertNotIn("Runtime destination check failed", result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.build.exists())


if __name__ == "__main__":
    unittest.main()
