from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import Mock, call, patch

from tools.qa.qa import cli
from tools.qa.qa.emu import key_mask
from tools.qa.qa.errors import QAError, UsageError
from tools.qa.qa.flows.smoke import map_id


class CliTests(unittest.TestCase):
    def test_epoch_is_utc_and_restricted_to_guest_year_range(self):
        self.assertEqual(cli.epoch("2026-01-01T12:00:00Z"), 1767268800000)
        self.assertEqual(cli.epoch("2026-01-01T13:00:00+01:00"), cli.epoch(None))
        self.assertEqual(cli.epoch("2026-01-01T12:00:00"), cli.epoch(None))
        for value in ("invalid", "1999-12-31", "2100-01-01"):
            with self.subTest(value=value), self.assertRaises(UsageError):
                cli.epoch(value)

    def test_parser_reports_usage_64_before_environment_access(self):
        with patch.object(cli, "git") as git, redirect_stderr(io.StringIO()):
            for argv in ([], ["run", "unknown"], ["step"], ["run", "smoke", "--scale", "0"]):
                with self.subTest(argv=argv):
                    self.assertEqual(cli.main(argv), 64)
            git.assert_not_called()

    def test_step_parses_explicit_chords_and_frame_counts(self):
        args = cli.parser().parse_args([
            "step", "--bundle", "bundle", "--state", "in.state", "--out", "out.state",
            "--tap", "A", "--tap", "R+START", "--wait", "30",
        ])
        self.assertEqual(args.tap, ["A", "R+START"])
        self.assertEqual((args.wait, args.scale, args.interval), (30, 4, 1))

    def test_key_chords_are_masks_not_key_indices(self):
        self.assertEqual(key_mask("a"), 1)
        self.assertEqual(key_mask("R + START"), 264)
        self.assertEqual(key_mask("UP+LEFT"), 96)
        for value in ("", "A+", "SPACE", "UNKNOWN+A"):
            with self.subTest(value=value), self.assertRaises(UsageError):
                key_mask(value)

    def test_generated_enum_map_identifier(self):
        header = "enum { \n MAP_INSIDE_OF_TRUCK = (40 | (25 << 8)),\n};"
        self.assertEqual(map_id(header, "MAP_INSIDE_OF_TRUCK"), (25, 40))
        for value in ("", header + "\n" + header,
                      "MAP_INSIDE_OF_TRUCK = (300 | (25 << 8)),"):
            with self.subTest(value=value), self.assertRaises(QAError):
                map_id(value, "MAP_INSIDE_OF_TRUCK")

    def test_outputs_cannot_escape_artifacts_or_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "qa-artifacts"
            root.mkdir()
            output = root / "steps/out.state"
            self.assertEqual(cli.output_path(output, root), output)
            output.write_bytes(b"existing")
            with self.assertRaises(UsageError):
                cli.output_path(output, root)
            with self.assertRaises(UsageError):
                cli.output_path(Path(directory) / "outside.state", root)
            self.assertEqual(output.read_bytes(), b"existing")

    def test_callback_error_scan_handles_chunk_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "native.log"
            log.write_bytes(b"x" * (1024 * 1024 - 10)
                            + b"Exception ignored from cffi callback")
            self.assertTrue(cli.callback_error(log))
            log.write_text("GBA: normal captured native output")
            self.assertFalse(cli.callback_error(log))

    def test_save_input_is_exactly_128k_without_trailer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.sav"
            path.write_bytes(b"\xff" * cli.SAVE_BYTES)
            self.assertEqual(len(cli.save_input(path)), cli.SAVE_BYTES)
            path.write_bytes(b"\xff" * (cli.SAVE_BYTES + 16))
            with self.assertRaisesRegex(UsageError, "no RTC trailer"):
                cli.save_input(path)

    def test_reaped_worker_with_no_children_needs_no_cleanup(self):
        process = Mock(pid=12345)
        with patch("os.killpg", side_effect=ProcessLookupError) as kill:
            self.assertFalse(cli.stop_group(process))
        kill.assert_called_once_with(12345, signal.SIGTERM)
        process.wait.assert_not_called()

    def test_surviving_owned_group_is_reported_and_killed(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("worker", 5), -9]
        with patch("os.killpg") as kill:
            self.assertTrue(cli.stop_group(process))
        self.assertEqual(kill.call_args_list, [
            call(12345, signal.SIGTERM), call(12345, signal.SIGKILL),
        ])

    def test_invalid_image_label_creates_no_run_or_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            args = cli.parser().parse_args([
                "run", "smoke", "--bundle", str(repo / "bundle"),
            ])
            bundle = SimpleNamespace(bundle_id="a" * 64, path=repo / "bundle")
            runtime = SimpleNamespace(identity={})
            with patch.object(cli.Bundle, "load", return_value=bundle):
                with patch.object(cli, "load_runtime", return_value=runtime):
                    with patch.dict(os.environ, {"QA_IMAGE_ID": "invalid"}):
                        with patch("subprocess.Popen") as process:
                            with self.assertRaises(UsageError):
                                cli.launch(args, repo)
                            process.assert_not_called()
            self.assertFalse((repo / "qa-artifacts").exists())

    def test_unsupported_zip_version_returns_three_without_starting_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            state = repo / "unsupported.state"
            entry = zipfile.ZipInfo("core.state")
            entry.extract_version = 100
            with zipfile.ZipFile(state, "w") as archive:
                archive.writestr(entry, b"unsupported")
            bundle = SimpleNamespace(bundle_id="a" * 64, path=repo / "bundle")
            with (
                patch.object(cli.Bundle, "load", return_value=bundle),
                patch.object(cli, "load_runtime", return_value=SimpleNamespace(identity={})),
                patch.object(cli, "git", return_value=SimpleNamespace(stdout=str(repo).encode())),
                patch.object(cli.subprocess, "Popen") as process,
                redirect_stderr(io.StringIO()) as error,
            ):
                code = cli.main([
                    "step", "--bundle", str(bundle.path), "--state", str(state),
                    "--out", str(repo / "qa-artifacts/next.state"),
                ])
            self.assertEqual(code, 3)
            self.assertIn("Cannot read checkpoint", error.getvalue())
            self.assertNotIn("Traceback", error.getvalue())
            process.assert_not_called()
            self.assertFalse((repo / "qa-artifacts").exists())


if __name__ == "__main__":
    unittest.main()
