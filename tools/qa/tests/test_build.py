import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.qa.qa import build
from tools.qa.qa.artifacts import write_json
from tools.qa.qa.errors import QAError, UsageError


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / ".gitignore").write_text("qa-artifacts/\nsecret.local\n")
        (self.repo / "data.txt").write_text("committed")
        self.git("add", ".")
        self.git("commit", "-q", "-m",
                 "Fixture\n\nCo-authored-by: Copilot "
                 "<223556219+Copilot@users.noreply.github.com>")
        self.head = self.git("rev-parse", "HEAD").stdout.decode().strip()

    def git(self, *args):
        environment = dict(os.environ)
        environment.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                            "GIT_OPTIONAL_LOCKS": "0"})
        return subprocess.run(
            ["git", "-c", "user.name=QA Fixture", "-c", "user.email=qa@invalid",
             "-c", "commit.gpgsign=false", "-C", str(self.repo), *args],
            env=environment, check=True, capture_output=True,
        )

    def directory(self, name="snapshot"):
        directory = self.repo / "qa-artifacts" / name
        directory.mkdir(parents=True)
        return directory

    def fake_make(self, source, log, jobs):
        for kind in ("rom", "elf", "sym"):
            (source / build.FILES[kind]).write_bytes(kind.encode())
        constants = source / "include/constants/map_groups.h"
        constants.parent.mkdir(parents=True)
        constants.write_text("#define MAP_INSIDE_OF_TRUCK (0 | (25 << 8))\n")
        log.write_text("fixture build output\n")

    def bundle(self):
        with patch.object(build, "run_make", side_effect=self.fake_make):
            with patch.object(build, "tool_version", return_value="fixture-tool"):
                return build.build_bundle(self.repo, ref="HEAD", jobs=1)

    def test_clean_snapshot_has_real_ancestry(self):
        directory = self.directory()
        source = build.capture_source(self.repo, directory)
        self.assertEqual(source["base_commit"], self.head)
        self.assertEqual((directory / "source/data.txt").read_text(), "committed")
        self.assertEqual(build.revision(directory / "source", "HEAD"), self.head)

    def test_dirty_snapshot_preserves_index_head_and_user_files(self):
        (self.repo / "data.txt").write_text("staged")
        self.git("add", "data.txt")
        (self.repo / "data.txt").write_text("working")
        (self.repo / "selected.txt").write_text("selected")
        (self.repo / "other.txt").write_text("not selected")
        index = (self.repo / ".git/index").read_bytes()
        directory = self.directory()
        source = build.capture_source(
            self.repo, directory, include_dirty=True, untracked=("selected.txt",)
        )
        self.assertEqual((directory / "source/data.txt").read_text(), "working")
        self.assertEqual((directory / "source/selected.txt").read_text(), "selected")
        self.assertFalse((directory / "source/other.txt").exists())
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.decode().strip(), self.head)
        self.assertEqual((self.repo / "data.txt").read_text(), "working")
        parent = self.git("rev-parse", source["snapshot_commit"] + "^").stdout.decode().strip()
        self.assertEqual(parent, self.head)

    def test_same_dirty_snapshot_has_reproducible_tree_and_commit(self):
        (self.repo / "data.txt").write_text("working")
        first = build.capture_source(self.repo, self.directory("a"), include_dirty=True)
        second = build.capture_source(self.repo, self.directory("b"), include_dirty=True)
        self.assertEqual(first["source_tree"], second["source_tree"])
        self.assertEqual(first["snapshot_commit"], second["snapshot_commit"])

    def test_staged_addition_captures_working_contents_without_touching_index(self):
        (self.repo / "new.txt").write_text("staged")
        self.git("add", "new.txt")
        (self.repo / "new.txt").write_text("working")
        (self.repo / "other.txt").write_text("untracked")
        index = (self.repo / ".git/index").read_bytes()
        directory = self.directory()
        build.capture_source(self.repo, directory, include_dirty=True)
        self.assertEqual((directory / "source/new.txt").read_text(), "working")
        self.assertFalse((directory / "source/other.txt").exists())
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(build.revision(self.repo, "HEAD"), self.head)
        self.assertEqual((self.repo / "new.txt").read_text(), "working")

    def test_staged_rename_captures_destination_without_touching_index(self):
        self.git("mv", "data.txt", "renamed.txt")
        (self.repo / "renamed.txt").write_text("working")
        (self.repo / "other.txt").write_text("untracked")
        index = (self.repo / ".git/index").read_bytes()
        directory = self.directory()
        build.capture_source(self.repo, directory, include_dirty=True)
        self.assertFalse((directory / "source/data.txt").exists())
        self.assertEqual((directory / "source/renamed.txt").read_text(), "working")
        self.assertFalse((directory / "source/other.txt").exists())
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(build.revision(self.repo, "HEAD"), self.head)
        self.assertFalse((self.repo / "data.txt").exists())
        self.assertEqual((self.repo / "renamed.txt").read_text(), "working")

    def test_newly_tracked_pathspec_characters_are_literal(self):
        name = "new[1].txt"
        (self.repo / name).write_text("selected")
        self.git("--literal-pathspecs", "add", "--", name)
        (self.repo / "new1.txt").write_text("untracked")
        directory = self.directory()
        build.capture_source(self.repo, directory, include_dirty=True)
        self.assertEqual((directory / "source" / name).read_text(), "selected")
        self.assertFalse((directory / "source/new1.txt").exists())

    def test_staged_addition_deleted_from_worktree_stays_absent(self):
        path = self.repo / "new.txt"
        path.write_text("staged")
        self.git("add", "new.txt")
        path.unlink()
        index = (self.repo / ".git/index").read_bytes()
        directory = self.directory()
        build.capture_source(self.repo, directory, include_dirty=True)
        self.assertFalse((directory / "source/new.txt").exists())
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_force_tracked_ignored_addition_is_included(self):
        path = self.repo / "secret.local"
        path.write_text("tracked fixture, not a secret")
        self.git("add", "--force", "--", path.name)
        index = (self.repo / ".git/index").read_bytes()
        directory = self.directory()
        build.capture_source(self.repo, directory, include_dirty=True)
        self.assertEqual((directory / "source/secret.local").read_text(), path.read_text())
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_untracking_keeps_working_file_but_excludes_it_from_snapshot(self):
        self.git("rm", "--cached", "data.txt")
        index = (self.repo / ".git/index").read_bytes()
        directory = self.directory()
        build.capture_source(self.repo, directory, include_dirty=True)
        self.assertFalse((directory / "source/data.txt").exists())
        self.assertEqual((self.repo / "data.txt").read_text(), "committed")
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_untracked_previous_file_can_be_explicitly_selected_again(self):
        self.git("rm", "--cached", "data.txt")
        (self.repo / "data.txt").write_text("selected working copy")
        index = (self.repo / ".git/index").read_bytes()
        directory = self.directory()
        build.capture_source(
            self.repo, directory, include_dirty=True, untracked=("data.txt",)
        )
        self.assertEqual(
            (directory / "source/data.txt").read_text(), "selected working copy"
        )
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_selected_untracked_edit_changes_identity(self):
        (self.repo / "new.txt").write_text("one")
        first = build.capture_source(
            self.repo, self.directory("a"), include_dirty=True, untracked=("new.txt",)
        )
        (self.repo / "new.txt").write_text("two")
        second = build.capture_source(
            self.repo, self.directory("b"), include_dirty=True, untracked=("new.txt",)
        )
        self.assertNotEqual(first["source_tree"], second["source_tree"])

    def test_untracked_pathspec_collision_uses_the_literal_filename(self):
        (self.repo / "new1.c").write_text("tracked file")
        self.git("add", "new1.c")
        (self.repo / "new[1].c").write_text("selected untracked file")
        (self.repo / "other.c").write_text("unselected")
        index = (self.repo / ".git/index").read_bytes()
        directory = self.directory()
        build.capture_source(
            self.repo, directory, include_dirty=True, untracked=("new[1].c",)
        )
        self.assertEqual(
            (directory / "source/new[1].c").read_text(), "selected untracked file"
        )
        self.assertEqual((directory / "source/new1.c").read_text(), "tracked file")
        self.assertFalse((directory / "source/other.c").exists())
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)

    def test_dirty_default_requires_explicit_choice(self):
        (self.repo / "data.txt").write_text("working")
        with self.assertRaisesRegex(UsageError, "Source tree is dirty"):
            build.capture_source(self.repo, self.directory())

    def test_explicit_ref_excludes_dirty_worktree(self):
        (self.repo / "data.txt").write_text("working")
        directory = self.directory()
        source = build.capture_source(self.repo, directory, ref=self.head)
        self.assertTrue(source["worktree_changes_excluded"])
        self.assertEqual((directory / "source/data.txt").read_text(), "committed")

    def test_untracked_selection_requires_dirty_option(self):
        with self.assertRaisesRegex(UsageError, "requires --include-dirty"):
            build.capture_source(self.repo, self.directory(), untracked=("new.txt",))

    def test_other_base_cannot_mix_with_dirty_worktree(self):
        (self.repo / "data.txt").write_text("next")
        self.git("add", ".")
        self.git("commit", "-q", "-m",
                 "Next\n\nCo-authored-by: Copilot "
                 "<223556219+Copilot@users.noreply.github.com>")
        with self.assertRaisesRegex(UsageError, "invoking HEAD"):
            build.capture_source(self.repo, self.directory(), ref=self.head, include_dirty=True)

    def test_ignored_and_outside_and_symlinked_inputs_are_rejected(self):
        (self.repo / "secret.local").write_text("ignored fixture")
        outside = self.root / "outside.txt"
        outside.write_text("outside")
        (self.repo / "link.txt").symlink_to(outside)
        for name in ("secret.local", str(outside), "link.txt", ".git", "data.txt"):
            with self.subTest(name=name), self.assertRaises(UsageError):
                build.selected_untracked(self.repo, (name,))

    def test_checkout_hooks_do_not_execute(self):
        marker = self.root / "hook-ran"
        hook = self.repo / ".git/hooks/post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        build.capture_source(self.repo, self.directory())
        self.assertFalse(marker.exists())

    def test_source_capture_race_stops_before_checkout(self):
        directory = self.directory()
        with patch.object(build, "fingerprint", side_effect=["before", "after"]):
            with self.assertRaisesRegex(QAError, "Source changed"):
                build.capture_source(self.repo, directory, include_dirty=True)
        self.assertFalse((directory / "source").exists())

    def test_bundle_hashes_every_required_artifact(self):
        bundle = self.bundle()
        self.assertEqual(set(bundle.manifest["identity"]["files"]), set(build.FILES))
        self.assertEqual(build.Bundle.load(bundle.path).bundle_id, bundle.bundle_id)
        self.assertFalse((self.repo / "pokeemerald.gba").exists())

    def test_bundle_refuses_tampered_rom(self):
        bundle = self.bundle()
        bundle.file("rom").write_bytes(b"tampered")
        with self.assertRaisesRegex(QAError, "integrity mismatch"):
            build.Bundle.load(bundle.path)

    def test_bundle_refuses_tampered_identity(self):
        bundle = self.bundle()
        manifest = bundle.manifest
        manifest["identity"]["source_tree"] = "different"
        write_json(bundle.path / "manifest.json", manifest, replace=True)
        with self.assertRaisesRegex(QAError, "Invalid or incomplete"):
            build.Bundle.load(bundle.path)

    def test_failed_build_remains_error_not_complete(self):
        with patch.object(build, "run_make", side_effect=QAError("fixture build failure")):
            with patch.object(build, "tool_version", return_value="fixture-tool"):
                with self.assertRaisesRegex(QAError, "fixture build failure"):
                    build.build_bundle(self.repo, ref="HEAD", jobs=1)
        path = next((self.repo / "qa-artifacts/bundles").iterdir())
        self.assertEqual(json.loads((path / "manifest.json").read_text())["status"], "error")
        with self.assertRaises(QAError):
            build.Bundle.load(path)

    def test_usage_error_keeps_usage_exit_code(self):
        (self.repo / "data.txt").write_text("working")
        with self.assertRaises(UsageError) as raised:
            build.build_bundle(self.repo, jobs=1)
        self.assertEqual(raised.exception.exit_code, 64)

    def test_atomic_json_does_not_overwrite_without_permission(self):
        path = self.root / "result.json"
        write_json(path, {"status": "incomplete"})
        with self.assertRaises(FileExistsError):
            write_json(path, {"status": "pass"})
        self.assertEqual(json.loads(path.read_text())["status"], "incomplete")
        self.assertFalse(list(self.root.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
