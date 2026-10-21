import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.qa.qa.errors import BehaviorFailure, PreconditionFailure, QAError, UsageError
from tools.qa.qa.results import Run, aggregate


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = Run.create(
            self.root / "run", operation="fixture", bundle_id="a" * 64,
            cases=[{"name": "first"}, {"name": "second"}],
        )
        self.run.start()
        self.addCleanup(self.close_events)

    def close_events(self):
        if self.run.events is not None:
            self.run.events.close()

    def test_initial_manifest_cannot_look_like_pass(self):
        manifest = json.loads((self.run.directory / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "incomplete")
        self.assertIsNone(manifest["exit_code"])
        self.assertFalse(manifest["publishable"])
        self.assertTrue(all(case["status"] == "not-run" for case in manifest["cases"]))

    def test_precedence_is_error_then_failure_then_inconclusive(self):
        for statuses, code in (
            (["pass", "pass"], 0), (["pass", "not-run"], 2),
            (["fail", "not-run"], 1), (["inconclusive", "fail"], 1),
            (["error", "fail"], 3), ([], 3),
        ):
            with self.subTest(statuses=statuses):
                self.assertEqual(aggregate([{"status": value} for value in statuses]), code)

    def test_completed_cases_and_verified_media_pass(self):
        for name in ("first", "second"):
            with self.run.case(name) as case:
                case["observed"] = {"value": 123}
        self.assertEqual(self.run.finish(media={"verified": True}), 0)
        self.assertTrue(self.run.manifest["publishable"])

    def test_precondition_is_inconclusive_and_later_case_not_run(self):
        error = PreconditionFailure("not ready")
        with self.assertRaises(PreconditionFailure):
            with self.run.case("first"):
                raise error
        self.assertEqual(self.run.finish(media={"verified": True}, error=error), 2)
        self.assertEqual(self.run.manifest["cases"][1]["status"], "not-run")

    def test_behavior_failure_is_not_hidden_by_unexecuted_case(self):
        error = BehaviorFailure("wrong callback")
        with self.assertRaises(BehaviorFailure):
            with self.run.case("first"):
                raise error
        self.assertEqual(self.run.finish(media={"verified": True}, error=error), 1)
        self.assertEqual(self.run.manifest["verdict"], "fail")

    def test_recording_error_overrides_behavior_failure(self):
        with self.assertRaises(BehaviorFailure):
            with self.run.case("first"):
                raise BehaviorFailure("wrong callback")
        self.assertEqual(self.run.finish(media=None, error=QAError("encoder failed")), 3)
        self.assertFalse(self.run.manifest["publishable"])

    def test_missing_recording_cannot_pass(self):
        for name in ("first", "second"):
            with self.run.case(name):
                pass
        self.assertEqual(self.run.finish(media=None), 3)
        self.assertFalse(self.run.manifest["publishable"])

    def test_usage_error_remains_usage_even_without_media(self):
        self.assertEqual(self.run.finish(media=None, error=UsageError("bad epoch")), 64)
        self.assertFalse(self.run.manifest["publishable"])

    def test_run_cannot_finalize_twice(self):
        self.run.finish(media=None)
        with self.assertRaisesRegex(QAError, "already been finalized"):
            self.run.finish(media={"verified": True})

    def test_case_cannot_run_twice(self):
        with self.run.case("first"):
            pass
        with self.assertRaisesRegex(QAError, "already executed"):
            with self.run.case("first"):
                pass

    def test_io_error_is_recorded_on_case_and_cannot_be_reentered(self):
        error = OSError("checkpoint write failed")
        with self.assertRaises(OSError) as raised:
            with self.run.case("first"):
                raise error
        self.assertIs(raised.exception, error)
        manifest = json.loads((self.run.directory / "manifest.json").read_text())
        self.assertEqual(manifest["cases"][0]["status"], "error")
        self.assertEqual(manifest["cases"][0]["error"], str(error))
        self.assertEqual(manifest["cases"][0]["end_frame"], 0)
        self.assertEqual(manifest["cases"][1]["status"], "not-run")
        with self.assertRaisesRegex(QAError, "already executed"):
            with self.run.case("first"):
                self.fail("An errored case must not be reentered")
        self.assertEqual(self.run.finish(media={"verified": True}), 3)
        self.assertFalse(self.run.manifest["publishable"])

    def test_ordinary_programming_error_preserves_original_exception(self):
        error = ValueError("invalid observation")
        with self.assertRaises(ValueError) as raised:
            with self.run.case("first"):
                raise error
        self.assertIs(raised.exception, error)
        self.assertEqual(self.run.manifest["cases"][0]["status"], "error")
        self.assertEqual(self.run.manifest["cases"][0]["error"], str(error))

    def test_case_start_log_failure_is_also_a_case_error(self):
        with patch.object(self.run, "event", side_effect=OSError("event write failed")):
            with self.assertRaisesRegex(OSError, "event write failed"):
                with self.run.case("first"):
                    self.fail("Case body must not execute after its start event fails")
        self.assertEqual(self.run.manifest["cases"][0]["status"], "error")
        self.assertEqual(self.run.manifest["cases"][1]["status"], "not-run")
