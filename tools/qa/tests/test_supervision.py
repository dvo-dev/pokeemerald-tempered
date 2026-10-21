from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.qa.qa import cli
from tools.qa.qa.artifacts import read_json, write_json
from tools.qa.qa.errors import QAError


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.previous = {number: signal.getsignal(number)
                         for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        self.addCleanup(self.restore_handlers)

    def restore_handlers(self):
        for number, handler in self.previous.items():
            signal.signal(number, handler)

    def assert_handlers_restored(self):
        self.assertEqual(
            {number: signal.getsignal(number) for number in self.previous}, self.previous
        )

    def supervise(self, phase=None, number=None):
        self.process = Mock(pid=12345, returncode=None)

        def notify():
            handler = signal.getsignal(number)
            self.assertTrue(callable(handler))
            handler(number, None)

        def start(command, **kwargs):
            self.directory = Path(command[-1]).parent
            self.assertTrue(kwargs["start_new_session"])
            if phase == "spawn-error":
                raise OSError("spawn failed")
            if phase == "spawn":
                notify()
            return self.process

        def wait(timeout):
            if phase == "wait-error":
                raise OSError("wait failed")
            if phase == "keyboard":
                raise KeyboardInterrupt
            if phase == "timeout":
                raise subprocess.TimeoutExpired("worker", timeout)
            if phase == "wait":
                notify()
            manifest = read_json(self.directory / "manifest.json")
            manifest.update(status="complete", exit_code=0, verdict="pass", publishable=True)
            for case in manifest["cases"]:
                case["status"] = "pass"
            write_json(self.directory / "manifest.json", manifest, replace=True)
            self.process.returncode = 0
            return 0

        def stop(process):
            self.assertIs(process, self.process)
            if phase == "cleanup":
                notify()
            if phase == "cleanup-error":
                raise OSError("cleanup failed")
            if process.returncode is None:
                process.returncode = -signal.SIGTERM
            return phase == "surviving"

        self.process.wait.side_effect = wait
        self.start = Mock(side_effect=start)
        self.stop = Mock(side_effect=stop)
        bundle = SimpleNamespace(bundle_id="a" * 64, path=self.root / "bundle")
        args = cli.parser().parse_args([
            "run", "smoke", "--bundle", str(bundle.path), "--timeout-seconds", "1",
        ])
        with (
            patch.object(cli.Bundle, "load", return_value=bundle),
            patch.object(cli, "load_runtime", return_value=SimpleNamespace(identity={})),
            patch.object(cli.subprocess, "Popen", self.start),
            patch.object(cli, "stop_group", self.stop),
            patch.dict(os.environ, {"QA_IMAGE_ID": "sha256:" + "0" * 64}),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.launch(args, self.root)
        response = json.loads(output.getvalue())
        self.assertEqual(response["exit_code"], code)
        return code, read_json(Path(response["manifest"]))

    def test_normal_completion_still_passes_and_restores_handlers(self):
        code, manifest = self.supervise()
        self.assertEqual(code, 0)
        self.assertTrue(manifest["publishable"])
        self.stop.assert_called_once_with(self.process)
        self.assert_handlers_restored()

    def test_signals_during_spawn_are_deferred_until_process_is_owned(self):
        for number in self.previous:
            with self.subTest(number=number):
                code, manifest = self.supervise("spawn", number)
                self.assertEqual(code, 3)
                self.assertFalse(manifest["publishable"])
                self.assertFalse(manifest["unexpected_child_processes"])
                self.assertEqual(manifest["parent_signal"], number)
                self.process.wait.assert_not_called()
                self.stop.assert_called_once_with(self.process)
                self.assert_handlers_restored()

    def test_signals_override_a_success_shaped_worker_result(self):
        for number in self.previous:
            with self.subTest(number=number):
                code, manifest = self.supervise("wait", number)
                self.assertEqual(code, 3)
                self.assertEqual(manifest["status"], "error")
                self.assertFalse(manifest["publishable"])
                self.assertTrue(manifest["worker_cancelled"])
                self.assertEqual(manifest["parent_signal"], number)
                self.assertEqual(manifest["worker_exit_code"], 0)
                self.assertEqual(manifest["worker_manifest_exit_code"], 0)
                self.assertEqual(manifest["worker_verdict"], "pass")
                self.stop.assert_called_once_with(self.process)
                self.assert_handlers_restored()

    def test_signal_during_cleanup_is_recorded_without_interrupting_cleanup(self):
        for number in self.previous:
            with self.subTest(number=number):
                code, manifest = self.supervise("cleanup", number)
                self.assertEqual(code, 3)
                self.assertEqual(manifest["parent_signal"], number)
                self.assertEqual(manifest["worker_verdict"], "pass")
                self.assertFalse(manifest["publishable"])
                self.stop.assert_called_once_with(self.process)
                self.assert_handlers_restored()

    def test_signal_before_launch_does_not_create_a_worker(self):
        original = signal.signal
        delivered = False

        def install(number, handler):
            nonlocal delivered
            previous = original(number, handler)
            if number == signal.SIGTERM and callable(handler) and not delivered:
                delivered = True
                handler(number, None)
            return previous

        with patch.object(cli.signal, "signal", side_effect=install):
            code, manifest = self.supervise()
        self.assertEqual(code, 3)
        self.assertEqual(manifest["parent_signal"], signal.SIGTERM)
        self.start.assert_not_called()
        self.stop.assert_not_called()
        self.assert_handlers_restored()

    def test_successful_worker_cannot_hide_a_surviving_encoder(self):
        code, manifest = self.supervise("surviving")
        self.assertEqual(code, 3)
        self.assertTrue(manifest["unexpected_child_processes"])
        self.assertEqual(manifest["worker_exit_code"], 0)
        self.assertFalse(manifest["publishable"])
        self.stop.assert_called_once_with(self.process)
        self.assert_handlers_restored()

    def test_spawn_failure_restores_handlers_without_cleanup_of_an_unowned_pid(self):
        with self.assertRaisesRegex(OSError, "spawn failed"):
            self.supervise("spawn-error")
        self.stop.assert_not_called()
        self.assert_handlers_restored()

    def test_wait_failure_still_cleans_up_and_restores_handlers(self):
        with self.assertRaisesRegex(OSError, "wait failed"):
            self.supervise("wait-error")
        self.stop.assert_called_once_with(self.process)
        self.assert_handlers_restored()

    def test_cleanup_failure_still_restores_handlers(self):
        with self.assertRaisesRegex(OSError, "cleanup failed"):
            self.supervise("cleanup-error")
        self.assert_handlers_restored()

    def test_timeout_still_stops_group_and_reports_infrastructure_error(self):
        with patch.object(cli.time, "monotonic", side_effect=[0.0, 0.0, 2.0]):
            code, manifest = self.supervise("timeout")
        self.assertEqual(code, 3)
        self.assertTrue(manifest["worker_timeout"])
        self.assertFalse(manifest["worker_cancelled"])
        self.assertFalse(manifest["publishable"])
        self.stop.assert_called_once_with(self.process)
        self.assert_handlers_restored()

    def test_programmatic_keyboard_interrupt_remains_a_cancellation(self):
        code, manifest = self.supervise("keyboard")
        self.assertEqual(code, 3)
        self.assertTrue(manifest["worker_cancelled"])
        self.assertFalse(manifest["unexpected_child_processes"])
        self.assertFalse(manifest["publishable"])
        self.stop.assert_called_once_with(self.process)
        self.assert_handlers_restored()

    def test_unknown_native_handler_is_rejected_without_overwriting_it(self):
        with patch.object(cli.signal, "getsignal", return_value=None):
            with patch.object(cli.signal, "signal") as install:
                with self.assertRaisesRegex(QAError, "externally installed signal"):
                    self.supervise()
                install.assert_not_called()
        self.start.assert_not_called()
        self.stop.assert_not_called()
        self.assert_handlers_restored()
        self.assertFalse((self.root / "qa-artifacts").exists())


PARENT = r"""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch
from tools.qa.qa import cli
from tools.qa.qa.artifacts import write_json

root, worker = map(Path, sys.argv[1:])
real_popen = subprocess.Popen
def start(command, **kwargs):
    process = real_popen([sys.executable, str(worker), str(root), command[-1]], **kwargs)
    write_json(root / "started.json", {"worker": process.pid})
    return process
bundle = SimpleNamespace(bundle_id="a" * 64, path=root / "bundle")
args = cli.parser().parse_args(["run", "smoke", "--bundle", str(bundle.path)])
os.environ.pop("QA_IMAGE_ID", None)
with patch.object(cli.Bundle, "load", return_value=bundle), \
     patch.object(cli, "load_runtime", return_value=SimpleNamespace(identity={})), \
     patch.object(cli.subprocess, "Popen", side_effect=start):
    raise SystemExit(cli.launch(args, root))
"""

WORKER = r"""
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

root, config = map(Path, sys.argv[1:])
manifest_path = config.parent / "manifest.json"
encoder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
def stop(number, frame):
    encoder.terminate()
    encoder.wait(timeout=5)
    manifest = json.loads(manifest_path.read_text())
    manifest.update(status="complete", exit_code=0, verdict="pass", publishable=True)
    for case in manifest["cases"]:
        case["status"] = "pass"
    manifest_path.write_text(json.dumps(manifest))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
ready = root / "ready.tmp"
ready.write_text(json.dumps({"encoder": encoder.pid}))
ready.replace(root / "ready.json")
while True:
    time.sleep(1)
"""


class SignalProcessTests(unittest.TestCase):
    @staticmethod
    def running(pid):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return False
        return state not in ("Z", "X")

    def test_real_parent_signals_stop_worker_and_encoder(self):
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(number=number), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                worker = root / "worker.py"
                worker.write_text(WORKER)
                environment = dict(os.environ)
                environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
                parent = subprocess.Popen(
                    [sys.executable, "-c", PARENT, str(root), str(worker)],
                    cwd=root, env=environment, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                )
                started = None
                try:
                    deadline = time.monotonic() + 15
                    while not ((root / "ready.json").exists()
                               and (root / "started.json").exists()):
                        if parent.poll() is not None:
                            stdout, stderr = parent.communicate()
                            self.fail(f"Supervisor exited before fixture startup: {stdout}\n{stderr}")
                        if time.monotonic() >= deadline:
                            self.fail("Timed out waiting for the owned worker fixture")
                        time.sleep(0.02)
                    started = read_json(root / "started.json")
                    encoder = read_json(root / "ready.json")["encoder"]
                    self.assertEqual(os.getpgid(started["worker"]), started["worker"])
                    self.assertEqual(os.getpgid(encoder), started["worker"])
                    parent.send_signal(number)
                    stdout, stderr = parent.communicate(timeout=15)
                    self.assertEqual(parent.returncode, 3, stderr)
                    result = json.loads(stdout)
                    manifest = read_json(Path(result["manifest"]))
                    self.assertFalse(manifest["publishable"])
                    self.assertTrue(manifest["worker_cancelled"])
                    self.assertEqual(manifest["parent_signal"], number)
                    self.assertEqual(manifest["worker_exit_code"], 0)
                    self.assertEqual(manifest["worker_verdict"], "pass")
                    self.assertFalse(self.running(started["worker"]))
                    self.assertFalse(self.running(encoder))
                finally:
                    if parent.poll() is None:
                        parent.kill()
                    parent.communicate(timeout=5)
                    if started is None and (root / "started.json").exists():
                        started = read_json(root / "started.json")
                    owned = [started["worker"]] if started is not None else []
                    if (root / "ready.json").exists():
                        owned.append(read_json(root / "ready.json")["encoder"])
                    if started is not None and any(self.running(pid) for pid in owned):
                        try:
                            os.killpg(started["worker"], signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
