import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from .artifacts import content_id, read_json, write_json
from .build import Bundle, build_bundle, git
from .checkpoint import MAX_EPOCH_MS, MIN_EPOCH_MS, SAVE_BYTES, read_checkpoint
from .errors import QAError, UsageError
from .flows import smoke
from .record import Recorder, screenshots
from .results import Run
from .runtime import load_runtime


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def positive(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an integer") from error
    if result < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def nonnegative(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an integer") from error
    if result < 0:
        raise argparse.ArgumentTypeError("cannot be negative")
    return result


def epoch(value: str | None) -> int:
    if value is None:
        return int(datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp()) * 1000
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise UsageError(f"Invalid ISO-8601 epoch: {value}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    result = int(parsed.timestamp() * 1000)
    if not MIN_EPOCH_MS <= result < MAX_EPOCH_MS:
        raise UsageError("Epoch must be between 2000 and 2099 UTC")
    return result


def parser() -> Parser:
    root = Parser(description="Deterministic in-game QA with official mGBA Python bindings")
    commands = root.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="freeze and build an isolated ROM/ELF bundle")
    build.add_argument("--ref")
    build.add_argument("--include-dirty", action="store_true")
    build.add_argument("--include-untracked", action="append", default=[])
    build.add_argument("--jobs", type=positive)
    run = commands.add_parser("run", help="execute a RAM-backed QA flow")
    run.add_argument("flow", choices=["smoke", *sorted(smoke.CONTROLS)])
    run.add_argument("--save", type=Path)
    step = commands.add_parser("step", help="restore, advance, and save a fresh-process checkpoint")
    step.add_argument("--state", type=Path, required=True)
    step.add_argument("--out", type=Path, required=True)
    step.add_argument("--shot", type=Path)
    step.add_argument("--tap", action="append", default=[])
    step.add_argument("--wait", type=nonnegative, default=30)
    for command in (run, step):
        command.add_argument("--bundle", type=Path, required=True)
        command.add_argument("--epoch")
        command.add_argument("--scale", type=positive, default=4)
        command.add_argument("--interval", type=positive, default=1)
        command.add_argument("--timeout-seconds", type=positive, default=600)
    return root


def output_path(path: Path, artifact_root: Path) -> Path:
    path = path.resolve()
    if not path.is_relative_to(artifact_root.resolve()):
        raise UsageError("QA outputs must stay under this checkout's qa-artifacts/")
    if path.exists() or path.is_symlink():
        raise UsageError(f"Refusing to overwrite an existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save_input(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(SAVE_BYTES + 1)
    if len(data) != SAVE_BYTES:
        raise UsageError("Save input must be exactly 128 KiB, with no RTC trailer")
    return data


def callback_error(path: Path) -> bool:
    markers = (b"Exception ignored from cffi callback",
               b"Exception ignored on calling ctypes callback")
    with path.open("rb") as stream:
        previous = b""
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            text = previous + block
            if any(marker in text for marker in markers):
                return True
            previous = text[-128:]
    return False


def stop_group(process: subprocess.Popen) -> bool:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # The worker can exit before its encoder, which remains in this private group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    process.wait(timeout=5)
    return True


def launch(args, repo: Path) -> int:
    from .emu import key_mask

    bundle = Bundle.load(args.bundle)
    runtime = load_runtime()
    if args.scale * 240 >= 2**31:
        raise UsageError("Scaled video dimensions exceed the encoder's integer range")
    artifact_root = repo / "qa-artifacts"
    taps = getattr(args, "tap", [])
    for chord in taps:
        key_mask(chord)
    if args.command == "step" and args.wait == 0 and not taps:
        raise UsageError("A step must advance a real frame before capturing post-load evidence")
    requested_epoch = epoch(args.epoch) if args.epoch is not None else None
    previous_handlers = {
        number: signal.getsignal(number)
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    if any(handler is None for handler in previous_handlers.values()):
        raise QAError("Cannot safely restore externally installed signal handlers")
    state = None
    state_hash = None
    if args.command == "step":
        state = args.state.resolve()
        checkpoint = read_checkpoint(
            state, bundle_id=bundle.bundle_id, runtime=runtime.identity,
            epoch_ms=requested_epoch,
        )
        state_hash = checkpoint.sha256
        chosen_epoch = checkpoint.manifest["epoch_ms"]
        out = output_path(args.out, artifact_root)
        shot = output_path(args.shot, artifact_root) if args.shot else None
        if shot is not None:
            output_path(shot.with_name(shot.stem + "@4x.png"), artifact_root)
        cases = [{
            "name": "advance-frames", "precondition": "verified compatible checkpoint",
            "event": {"tap": taps, "wait": args.wait},
            "expected": "released inputs, fresh evidence, and complete next checkpoint",
            "max_frames": args.wait + len(taps) * 240,
        }]
    else:
        chosen_epoch = requested_epoch if requested_epoch is not None else epoch(None)
        out = shot = None
        cases = smoke.CASES if args.flow == "smoke" else smoke.CONTROL_CASES
    initial_save = getattr(args, "save", None)
    initial_hash = None
    if initial_save is not None:
        initial_save = initial_save.resolve()
        initial_hash = hashlib.sha256(save_input(initial_save)).hexdigest()
    image_id = os.environ.get("QA_IMAGE_ID")
    if image_id is not None:
        import re
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise UsageError("QA_IMAGE_ID must be a sha256 image identifier")
    directory = artifact_root / "runs" / uuid.uuid4().hex
    Run.create(directory, operation=args.command, bundle_id=bundle.bundle_id, cases=cases)
    config = {
        "schema_version": 1, "run_dir": str(directory), "operation": args.command,
        "bundle": str(bundle.path), "bundle_id": bundle.bundle_id,
        "runtime_id": content_id(runtime.identity), "epoch_ms": chosen_epoch,
        "scale": args.scale, "interval": args.interval,
        "state": str(state) if state else None, "state_sha256": state_hash,
        "out": str(out) if out else None, "shot": str(shot) if shot else None,
        "save": str(initial_save) if initial_save else None, "save_sha256": initial_hash,
        "flow": getattr(args, "flow", None), "taps": taps, "wait": getattr(args, "wait", 0),
        "image_id": image_id,
    }
    write_json(directory / "config.json", config)
    home = directory / "home"
    (home / ".config").mkdir(parents=True, mode=0o700)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "TZ": "UTC", "LANG": "C.UTF-8", "MALLOC_PERTURB_": "165",
        "PYTHONPYCACHEPREFIX": str(home / "pycache"),
    }
    command = [sys.executable, "-P", "-X", "faulthandler", "-u", "-m", "qa",
               "_worker", str(directory / "config.json")]
    log_path = directory / "native.log"
    timed_out = False
    cancelled = False
    parent_signal = None
    process = None
    code = 3
    surviving_children = False
    def request_cancel(number, _frame):
        nonlocal parent_signal
        if parent_signal is None:
            parent_signal = number

    try:
        for number in previous_handlers:
            signal.signal(number, request_cancel)
        with log_path.open("xb") as log:
            if parent_signal is None:
                process = subprocess.Popen(
                    command, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                deadline = time.monotonic() + args.timeout_seconds
                try:
                    while parent_signal is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        try:
                            code = process.wait(timeout=min(0.1, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            continue
                except KeyboardInterrupt:
                    cancelled = True
    finally:
        try:
            if process is not None:
                worker_finished = process.returncode is not None
                group_existed = stop_group(process)
                surviving_children = worker_finished and group_existed
        finally:
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)
    cancelled = cancelled or parent_signal is not None
    manifest = read_json(directory / "manifest.json")
    broken = (timed_out or cancelled or surviving_children or code not in (0, 1, 2, 3, 64)
              or manifest.get("exit_code") != code
              or manifest.get("status") not in ("complete", "error")
              or callback_error(log_path))
    if broken:
        manifest.update({
            "status": "error", "exit_code": 3, "verdict": "error", "publishable": False,
            "worker_exit_code": process.returncode if process is not None else None,
            "worker_manifest_exit_code": manifest.get("exit_code"),
            "worker_verdict": manifest.get("verdict"),
            "parent_signal": parent_signal, "worker_timeout": timed_out,
            "worker_cancelled": cancelled, "unexpected_child_processes": surviving_children,
            "error": "Worker failed, was cancelled, or logged a callback exception; inspect native.log",
        })
        write_json(directory / "manifest.json", manifest, replace=True)
        code = 3
    print(json.dumps({"run_dir": str(directory), "manifest": str(directory / "manifest.json"),
                      "exit_code": code, "verdict": manifest.get("verdict")}))
    return code


def worker(path: Path) -> int:
    from elftools.common.exceptions import DWARFError, ELFError
    from .emu import Session

    config = read_json(path)
    if config.get("schema_version") != 1 or config.get("operation") not in ("run", "step"):
        raise QAError("Invalid worker configuration")
    run = Run.load(Path(config["run_dir"]))
    if run.directory.resolve() != path.parent.resolve():
        raise QAError("Worker configuration is outside its declared run directory")
    home = run.directory / "home"
    (home / ".config").mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), TZ="UTC")
    run.manifest["image_id"] = config.get("image_id")
    run.start()
    session = recorder = None
    media = None
    error = None

    def note(problem: QAError) -> None:
        nonlocal error
        run.manifest.setdefault("errors", []).append(
            {"exit_code": problem.exit_code, "message": str(problem)}
        )
        precedence = {0: 0, 2: 1, 1: 2, 3: 3, 64: 4}
        if error is None or precedence[problem.exit_code] > precedence[error.exit_code]:
            error = problem

    try:
        bundle = Bundle.load(Path(config["bundle"]))
        runtime = load_runtime()
        if (bundle.bundle_id != config["bundle_id"]
                or content_id(runtime.identity) != config["runtime_id"]):
            raise QAError("Bundle or runtime changed between preflight and execution")
        initial = None
        if config["save"]:
            initial = save_input(Path(config["save"]))
            if hashlib.sha256(initial).hexdigest() != config["save_sha256"]:
                raise QAError("Initial save changed between preflight and execution")
        session = Session(bundle, runtime, run, config["epoch_ms"], initial)
        if config["state"]:
            session.restore(Path(config["state"]), expected_sha256=config["state_sha256"])
        recorder = Recorder(
            run.directory, start_frame=session.frame, fps=session.fps,
            scale=config["scale"], interval=config["interval"],
        )
        session.recorder = recorder
        if config["operation"] == "run":
            smoke.execute(session, run, config["flow"])
            checkpoint = session.save(run.directory / "final.state")
            run.manifest["checkpoint"] = {
                "file": str(run.directory / "final.state"),
                "sha256": checkpoint.sha256, "publishable": False,
            }
        else:
            with run.case("advance-frames") as case:
                for chord in config["taps"]:
                    session.tap(chord)
                session.advance(config["wait"])
                result = session.save(Path(config["out"]))
                run.manifest["checkpoint"] = {
                    "file": config["out"], "sha256": result.sha256, "publishable": False,
                }
                case["observed"] = session.observe()
                case["game"] = smoke.Oracles(session).observe()
    except QAError as problem:
        note(problem)
    except (OSError, ValueError, ELFError, DWARFError, subprocess.SubprocessError) as problem:
        note(QAError(str(problem)))
    finally:
        if session is not None:
            try:
                if session.last_frame is not None:
                    shot = Path(config["shot"]) if config["shot"] else run.directory / "final.png"
                    run.manifest["screenshots"].extend(screenshots(shot, session.rgbx()))
            except (QAError, OSError) as problem:
                note(problem if isinstance(problem, QAError) else QAError(str(problem)))
            finally:
                session.close()
        if recorder is not None:
            try:
                media = recorder.finish()
            except (QAError, OSError) as problem:
                note(problem if isinstance(problem, QAError) else QAError(str(problem)))
    return run.finish(media=media, error=error)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "_worker":
            if len(argv) != 2:
                raise UsageError("Worker requires exactly one configuration path")
            return worker(Path(argv[1]))
        args = parser().parse_args(argv)
        repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.decode().strip())
        if args.command == "build":
            bundle = build_bundle(
                repo, ref=args.ref, include_dirty=args.include_dirty,
                untracked=tuple(args.include_untracked), jobs=args.jobs,
            )
            print(json.dumps({"bundle": str(bundle.path), "bundle_id": bundle.bundle_id}))
            return 0
        return launch(args, repo)
    except QAError as error:
        print(f"qa: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"qa: infrastructure error: {error}", file=sys.stderr)
        return 3
