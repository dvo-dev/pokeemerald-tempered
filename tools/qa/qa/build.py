from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from .artifacts import content_id, read_json, sha256, write_json
from .errors import QAError, UsageError


BUILD_ARGS = (
    "DINFO=1", "DEBUG=0", "RELEASE=0", "TEST=0", "LTO=0",
    "GAME_VERSION=EMERALD", "all", "syms",
)
FILES = {
    "rom": "pokeemerald.gba",
    "elf": "pokeemerald.elf",
    "sym": "pokeemerald.sym",
    "constants": "map_groups.h",
    "log": "build.log",
}


def git(repo: Path, *args: str, env: dict | None = None,
        allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    if env:
        environment.update(env)
    command = ["git", "-c", "core.hooksPath=/dev/null",
               "-c", "commit.gpgsign=false", "-C", str(repo), *args]
    result = subprocess.run(command, env=environment, capture_output=True)
    if result.returncode not in allowed:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise QAError(f"Git command failed ({result.returncode}): {args}: {detail}")
    return result


def revision(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options",
               f"{ref}^{{commit}}").stdout.decode().strip()


def harness_id() -> str:
    root = Path(__file__).resolve().parents[1]
    records = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".toml", ".lock", ".patch", ".sh"}:
            records[path.relative_to(root).as_posix()] = sha256(path)
    return content_id(records)


def selected_untracked(repo: Path, names: tuple[str, ...]) -> list[str]:
    selected = []
    for name in names:
        supplied = Path(name)
        candidate = supplied if supplied.is_absolute() else repo / supplied
        try:
            relative = candidate.resolve().relative_to(repo)
        except ValueError as error:
            raise UsageError(f"Untracked source is outside the repository: {name}") from error
        if (not relative.parts or relative.parts[0] in {".git", "qa-artifacts"}
                or candidate.is_symlink() or not candidate.is_file()):
            raise UsageError(f"Select a regular untracked source file: {name}")
        if candidate.resolve() != Path(os.path.abspath(candidate)):
            raise UsageError(f"Symlinked untracked source is not supported: {name}")
        current = repo
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise UsageError(f"Symlinked untracked source is not supported: {name}")
        path = relative.as_posix()
        # check-ignore consumes filenames and rejects literal pathspec magic.
        ignored = git(repo, "check-ignore", "--quiet", "--", path, allowed=(0, 1))
        if ignored.returncode == 0:
            raise UsageError(f"Ignored files cannot enter a source snapshot: {name}")
        tracked = git(repo, "--literal-pathspecs", "ls-files", "--error-unmatch", "--",
                      path, allowed=(0, 1))
        if tracked.returncode == 0:
            raise UsageError(f"--include-untracked names a tracked file: {name}")
        selected.append(path)
    return sorted(set(selected))


def fingerprint(repo: Path, selected: list[str]) -> str:
    digest = hashlib.sha256(git(
        repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"
    ).stdout)
    for name in selected:
        path = repo / name
        if path.is_symlink() or not path.is_file():
            raise QAError(f"Source changed during capture: {name}")
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(str(path.stat().st_mode).encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def capture_source(repo: Path, directory: Path, *, ref: str | None = None,
                   include_dirty: bool = False,
                   untracked: tuple[str, ...] = ()) -> dict:
    head = revision(repo, "HEAD")
    base = revision(repo, ref or "HEAD")
    if untracked and not include_dirty:
        raise UsageError("--include-untracked requires --include-dirty")
    if include_dirty and base != head:
        raise UsageError("Dirty snapshots must be based on the invoking HEAD")
    status = git(repo, "status", "--porcelain=v1", "-z").stdout
    if not include_dirty and ref is None and status:
        raise UsageError(
            "Source tree is dirty. Select --include-dirty, or explicitly select "
            "--ref to build committed code without these edits."
        )
    selected = selected_untracked(repo, untracked)
    if include_dirty and git(
        repo, "diff", "--name-only", "--diff-filter=U", "-z"
    ).stdout:
        raise UsageError("Resolve merge conflicts before capturing dirty sources")
    before = fingerprint(repo, selected) if include_dirty else None
    commit = base
    tree = git(repo, "rev-parse", f"{base}^{{tree}}").stdout.decode().strip()
    if include_dirty:
        added = [
            os.fsdecode(name) for name in git(
                repo, "diff", "--name-only", "--no-renames", "--diff-filter=A", "-z",
                "HEAD", "--",
            ).stdout.split(b"\0") if name
        ]
        removed = [
            os.fsdecode(name) for name in git(
                repo, "diff", "--name-only", "--no-renames", "--diff-filter=D", "-z",
                "HEAD", "--",
            ).stdout.split(b"\0") if name
        ]
        environment = {
            "GIT_INDEX_FILE": str(directory / "source.index"),
            "GIT_LITERAL_PATHSPECS": "1",
        }
        git(repo, "read-tree", base, env=environment)
        git(repo, "add", "-u", "--", ".", env=environment)
        if removed:
            git(repo, "rm", "--cached", "--ignore-unmatch", "--", *removed, env=environment)
        if added:
            git(repo, "add", "--force", "--", *added, env=environment)
        if selected:
            git(repo, "add", "--", *selected, env=environment)
        tree = git(repo, "write-tree", env=environment).stdout.decode().strip()
        environment.update({
            "GIT_AUTHOR_NAME": "QA Snapshot",
            "GIT_AUTHOR_EMAIL": "qa-snapshot@invalid",
            "GIT_COMMITTER_NAME": "QA Snapshot",
            "GIT_COMMITTER_EMAIL": "qa-snapshot@invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
        })
        commit = git(
            repo, "commit-tree", tree, "-p", base, "-m",
            "QA source snapshot\n\n"
            "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
            env=environment,
        ).stdout.decode().strip()
        if before != fingerprint(repo, selected) or revision(repo, "HEAD") != head:
            raise QAError("Source changed during snapshot capture; no build was started")
    git(repo, "worktree", "add", "--detach", str(directory / "source"), commit)
    return {
        "base_commit": base, "source_tree": tree, "snapshot_commit": commit,
        "include_dirty": include_dirty, "untracked": selected,
        "worktree_changes_excluded": bool(status) and not include_dirty,
    }


def tool_version(command: str) -> str:
    result = subprocess.run([command, "--version"], capture_output=True, text=True)
    if result.returncode:
        raise QAError(f"Cannot identify toolchain: {command}: {result.stderr.strip()}")
    return result.stdout.splitlines()[0]


def run_make(source: Path, log: Path, jobs: int) -> None:
    environment = {
        key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ
    }
    environment.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    with log.open("xb") as stream:
        result = subprocess.run(
            ["make", *BUILD_ARGS, f"-j{jobs}"], cwd=source, env=environment,
            stdout=stream, stderr=subprocess.STDOUT,
        )
    if result.returncode:
        raise QAError(f"Bundle build failed ({result.returncode}); see {log}")


@dataclass(frozen=True)
class Bundle:
    path: Path
    manifest: dict

    @property
    def bundle_id(self) -> str:
        return self.manifest["bundle_id"]

    def file(self, kind: str) -> Path:
        return self.path / self.manifest["identity"]["files"][kind]["name"]

    @classmethod
    def load(cls, path: Path) -> "Bundle":
        path = path.resolve()
        manifest = read_json(path / "manifest.json")
        identity = manifest.get("identity")
        if (manifest.get("schema_version") != 1 or manifest.get("status") != "complete"
                or not isinstance(identity, dict)
                or content_id(identity) != manifest.get("bundle_id")):
            raise QAError(f"Invalid or incomplete build bundle: {path}")
        files = identity.get("files")
        if not isinstance(files, dict) or set(files) != set(FILES):
            raise QAError(f"Bundle has an invalid artifact set: {path}")
        for kind, expected_name in FILES.items():
            record = files[kind]
            if not isinstance(record, dict) or record.get("name") != expected_name:
                raise QAError(f"Bundle has an invalid {kind} artifact record")
            artifact = path / expected_name
            if (artifact.is_symlink() or not artifact.is_file()
                    or artifact.stat().st_size != record.get("bytes")
                    or sha256(artifact) != record.get("sha256")):
                raise QAError(f"Bundle integrity mismatch: {artifact}")
        return cls(path, manifest)


def build_bundle(repo: Path, *, ref: str | None = None, include_dirty: bool = False,
                 untracked: tuple[str, ...] = (), jobs: int | None = None) -> Bundle:
    repo = Path(git(repo.resolve(), "rev-parse", "--show-toplevel").stdout.decode().strip())
    jobs = jobs if jobs is not None else len(os.sched_getaffinity(0))
    if jobs < 1:
        raise UsageError("--jobs must be positive")
    harness_sha256 = harness_id()
    directory = repo / "qa-artifacts" / "bundles" / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    manifest = {"schema_version": 1, "status": "incomplete"}
    write_json(directory / "manifest.json", manifest)
    try:
        source = capture_source(
            repo, directory, ref=ref, include_dirty=include_dirty, untracked=untracked
        )
        versions = {
            name: tool_version(name) for name in
            ("make", "arm-none-eabi-gcc", "arm-none-eabi-ld", "gcc")
        }
        run_make(directory / "source", directory / "build.log", jobs)
        for kind in ("rom", "elf", "sym"):
            shutil.copyfile(directory / "source" / FILES[kind], directory / FILES[kind])
        shutil.copyfile(
            directory / "source/include/constants/map_groups.h", directory / FILES["constants"]
        )
        identity = {
            "base_commit": source["base_commit"],
            "source_tree": source["source_tree"],
            "build_args": list(BUILD_ARGS),
            "toolchain": versions,
            "harness_sha256": harness_sha256,
            "files": {
                kind: {"name": name, "bytes": (directory / name).stat().st_size,
                       "sha256": sha256(directory / name)}
                for kind, name in FILES.items()
            },
        }
        manifest.update({
            "status": "complete", "source": source,
            "identity": identity, "bundle_id": content_id(identity),
        })
        write_json(directory / "manifest.json", manifest, replace=True)
    except (QAError, OSError) as error:
        manifest.update({"status": "error", "error": str(error)})
        write_json(directory / "manifest.json", manifest, replace=True)
        if isinstance(error, QAError):
            raise
        raise QAError(f"Cannot build bundle in {directory}: {error}") from error
    return Bundle.load(directory)
