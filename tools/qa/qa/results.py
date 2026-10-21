from contextlib import contextmanager
import json
from pathlib import Path

from .artifacts import read_json, sha256, write_json
from .errors import QAError


CASE_CODES = {"pass": 0, "fail": 1, "inconclusive": 2, "not-run": 2, "error": 3}
VERDICTS = {0: "pass", 1: "fail", 2: "inconclusive", 3: "error", 64: "usage-error"}


def aggregate(cases: list[dict]) -> int:
    if not cases:
        return 3
    codes = [CASE_CODES[case["status"]] for case in cases]
    for code in (3, 1, 2):
        if code in codes:
            return code
    return 0


class Run:
    def __init__(self, directory: Path, manifest: dict):
        self.directory = directory
        self.manifest = manifest
        self.events = None

    @classmethod
    def create(cls, directory: Path, *, operation: str, bundle_id: str,
               cases: list[dict]) -> "Run":
        directory.mkdir(parents=True, mode=0o700)
        names = [case["name"] for case in cases]
        if len(names) != len(set(names)):
            raise QAError("Run contains duplicate case names")
        manifest = {
            "schema_version": 1, "status": "incomplete", "operation": operation,
            "bundle_id": bundle_id, "runtime": None, "exit_code": None,
            "publishable": False, "initial_frame": 0, "current_frame": 0,
            "frames_advanced": 0, "inputs": [], "screenshots": [],
            "cases": [dict(case, status="not-run") for case in cases],
        }
        write_json(directory / "manifest.json", manifest)
        return cls(directory, manifest)

    @classmethod
    def load(cls, directory: Path) -> "Run":
        manifest = read_json(directory / "manifest.json")
        if manifest.get("schema_version") != 1 or manifest.get("status") != "incomplete":
            raise QAError("Only an incomplete run can be opened for execution")
        return cls(directory, manifest)

    def start(self) -> None:
        self.events = (self.directory / "events.jsonl").open("x", encoding="utf-8")

    def flush(self) -> None:
        write_json(self.directory / "manifest.json", self.manifest, replace=True)

    def event(self, kind: str, **values) -> None:
        if self.events is None or self.events.closed:
            raise QAError("Run event log is not open")
        record = {"kind": kind, "frame": self.manifest["current_frame"], **values}
        self.events.write(json.dumps(record, sort_keys=True) + "\n")
        self.events.flush()

    @contextmanager
    def case(self, name: str):
        matches = [case for case in self.manifest["cases"] if case["name"] == name]
        if len(matches) != 1 or matches[0]["status"] != "not-run":
            raise QAError(f"Unknown or already executed case: {name}")
        case = matches[0]
        case["start_frame"] = self.manifest["current_frame"]
        try:
            self.event("case-start", name=name)
            yield case
        except QAError as error:
            case["status"] = {1: "fail", 2: "inconclusive", 3: "error", 64: "error"}[
                error.exit_code
            ]
            case["error"] = str(error)
            raise
        except Exception as error:
            case["status"] = "error"
            case["error"] = str(error)
            raise
        else:
            case["status"] = "pass"
        finally:
            case["end_frame"] = self.manifest["current_frame"]
            self.flush()

    def finish(self, *, media: dict | None, error: QAError | None = None) -> int:
        if self.manifest["status"] != "incomplete":
            raise QAError("Run has already been finalized")
        if self.events is not None and not self.events.closed:
            self.events.close()
        code = aggregate(self.manifest["cases"])
        if error is not None:
            self.manifest["error"] = str(error)
            if error.exit_code in (3, 64):
                code = error.exit_code
            elif code != 3:
                code = 1 if 1 in (code, error.exit_code) else 2
        if code != 64 and (not media or media.get("verified") is not True):
            code = 3
            self.manifest["evidence_error"] = "Requested recording was not verified"
        self.manifest.update({
            "status": "complete" if code in (0, 1, 2) else "error",
            "exit_code": code, "verdict": VERDICTS[code],
            "video": media, "publishable": code in (0, 1, 2),
        })
        event_path = self.directory / "events.jsonl"
        if event_path.is_file():
            self.manifest["events_sha256"] = sha256(event_path)
        self.flush()
        return code
