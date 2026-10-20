from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile

from .artifacts import content_id, write_bytes
from .errors import QAError, UsageError


SAVE_BYTES = 0x20000
MAX_CORE_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = MAX_CORE_BYTES + SAVE_BYTES + MAX_MANIFEST_BYTES + 4096
MIN_EPOCH_MS = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()) * 1000
MAX_EPOCH_MS = int(datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp()) * 1000
COMPONENTS = {"core.state", "save.bin", "manifest.json"}
MANIFEST_FIELDS = {
    "schema_version", "bundle_id", "runtime", "runtime_id", "frame", "epoch_ms",
    "keys", "parent_sha256", "components", "observations",
}


def valid_integer(value, minimum: int, maximum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value < maximum


def validate_manifest(manifest: dict) -> None:
    if (set(manifest) != MANIFEST_FIELDS
            or not valid_integer(manifest["schema_version"], 1, 2)):
        raise QAError("Unsupported or malformed checkpoint manifest")
    if not isinstance(manifest["bundle_id"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", manifest["bundle_id"]
    ):
        raise QAError("Invalid checkpoint bundle identity")
    if (not isinstance(manifest["runtime"], dict) or not manifest["runtime"]
            or manifest["runtime_id"] != content_id(manifest["runtime"])):
        raise QAError("Invalid checkpoint runtime identity")
    if not valid_integer(manifest["frame"], 0, 2**32):
        raise QAError("Invalid checkpoint frame counter")
    if not valid_integer(manifest["epoch_ms"], MIN_EPOCH_MS, MAX_EPOCH_MS):
        raise QAError("Checkpoint epoch must be between 2000 and 2099 UTC")
    if not valid_integer(manifest["keys"], 0, 1):
        raise QAError("Checkpoint requires released keys")
    parent = manifest["parent_sha256"]
    if parent is not None and (
        not isinstance(parent, str) or not re.fullmatch(r"[0-9a-f]{64}", parent)
    ):
        raise QAError("Invalid parent checkpoint hash")
    if not isinstance(manifest["observations"], dict):
        raise QAError("Invalid checkpoint observations")
    components = manifest["components"]
    if not isinstance(components, dict) or set(components) != {"core.state", "save.bin"}:
        raise QAError("Invalid checkpoint component records")
    for name, record in components.items():
        maximum = MAX_CORE_BYTES + 1 if name == "core.state" else SAVE_BYTES + 1
        minimum = 1 if name == "core.state" else SAVE_BYTES
        if (not isinstance(record, dict) or set(record) != {"bytes", "sha256"}
                or not valid_integer(record["bytes"], minimum, maximum)
                or not isinstance(record["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
            raise QAError(f"Invalid checkpoint component: {name}")


@dataclass(frozen=True)
class Checkpoint:
    core_state: bytes
    savedata: bytes
    manifest: dict
    sha256: str


def write_checkpoint(
    path: Path, *, bundle_id: str, runtime: dict, frame: int, epoch_ms: int,
    core_state: bytes, savedata: bytes, parent_sha256: str | None = None,
    observations: dict | None = None,
) -> Checkpoint:
    if not 0 < len(core_state) <= MAX_CORE_BYTES or len(savedata) != SAVE_BYTES:
        raise QAError("Checkpoint requires a bounded core state and exactly 128 KiB of save data")
    payloads = {"core.state": core_state, "save.bin": savedata}
    manifest = {
        "schema_version": 1, "bundle_id": bundle_id,
        "runtime": runtime, "runtime_id": content_id(runtime),
        "frame": frame, "epoch_ms": epoch_ms, "keys": 0,
        "parent_sha256": parent_sha256,
        "observations": observations if observations is not None else {},
        "components": {
            name: {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
            for name, value in payloads.items()
        },
    }
    validate_manifest(manifest)
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise QAError("Checkpoint manifest exceeds its size limit")
    payloads["manifest.json"] = encoded
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in payloads.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(info, value)
    encoded_archive = buffer.getvalue()
    write_bytes(path, encoded_archive)
    return Checkpoint(
        core_state, savedata, manifest, hashlib.sha256(encoded_archive).hexdigest()
    )


def read_checkpoint(
    path: Path, *, bundle_id: str, runtime: dict, epoch_ms: int | None = None,
) -> Checkpoint:
    try:
        with path.open("rb") as stream:
            encoded_archive = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(encoded_archive) > MAX_ARCHIVE_BYTES:
            raise QAError("Checkpoint archive exceeds its size limit")
        with zipfile.ZipFile(io.BytesIO(encoded_archive)) as archive:
            entries = archive.infolist()
            if len(entries) != 3 or {entry.filename for entry in entries} != COMPONENTS:
                raise QAError("Checkpoint must contain exactly the three declared components")
            limits = {"core.state": MAX_CORE_BYTES, "save.bin": SAVE_BYTES,
                      "manifest.json": MAX_MANIFEST_BYTES}
            for entry in entries:
                if (entry.is_dir() or entry.flag_bits & 1
                        or entry.compress_type != zipfile.ZIP_STORED
                        or entry.file_size > limits[entry.filename]):
                    raise QAError(f"Invalid or oversized checkpoint member: {entry.filename}")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict):
                raise QAError("Checkpoint manifest must be an object")
            validate_manifest(manifest)
            if manifest["bundle_id"] != bundle_id:
                raise QAError("Checkpoint belongs to a different build bundle")
            if manifest["runtime"] != runtime:
                raise QAError("Checkpoint belongs to a different emulator/runtime")
            if epoch_ms is not None and epoch_ms != manifest["epoch_ms"]:
                raise UsageError("Checkpoint clock wins; a conflicting epoch is not allowed")
            values = {}
            for name in ("core.state", "save.bin"):
                value = archive.read(name)
                record = manifest["components"][name]
                if (len(value) != record["bytes"]
                        or hashlib.sha256(value).hexdigest() != record["sha256"]):
                    raise QAError(f"Checkpoint integrity mismatch: {name}")
                values[name] = value
    except (OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        raise QAError(f"Cannot read checkpoint {path}: {error}") from error
    return Checkpoint(
        values["core.state"], values["save.bin"], manifest,
        hashlib.sha256(encoded_archive).hexdigest(),
    )
