from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tools.qa.qa.emu import Session
from tools.qa.qa.errors import BehaviorFailure, PreconditionFailure, UsageError


class InputTests(unittest.TestCase):
    def session(self, polled=True):
        session = object.__new__(Session)
        session.closed = False
        session.key_reads = 0
        session.core = SimpleNamespace(frame_counter=0, mask=0)
        session.core.set_keys = lambda *, raw: setattr(session.core, "mask", raw)
        session.native = SimpleNamespace(getKeys=lambda native: session.core.mask)
        session.memory = SimpleNamespace(read=lambda name: session.core.mask)
        session.run = SimpleNamespace(manifest={"inputs": []}, event=Mock())

        def advance(frames=1):
            session.core.frame_counter += frames
            if polled:
                session.key_reads += frames

        session.advance = advance
        return session

    def test_consecutive_taps_always_include_polled_release_gap(self):
        session = self.session()
        session.tap("A")
        session.tap("A")
        self.assertEqual(session.frame, 8)
        self.assertEqual(session.core.mask, 0)
        self.assertEqual(session.run.manifest["inputs"], [
            {"frame": 0, "keys": 1}, {"frame": 2, "keys": 0},
            {"frame": 4, "keys": 1}, {"frame": 6, "keys": 0},
        ])

    def test_unpolled_input_is_bounded_not_retried(self):
        session = self.session(polled=False)
        with self.assertRaises(BehaviorFailure):
            session.tap("A")
        self.assertEqual(session.frame, 120)

    def test_wait_requires_consecutive_stable_frames(self):
        session = self.session()
        values = iter([True, False, True, True])
        session.wait_until(lambda: next(values), 4, stable=2)
        self.assertEqual(session.frame, 4)

    def test_precondition_and_post_event_timeouts_are_distinct(self):
        for precondition, expected in ((True, PreconditionFailure), (False, BehaviorFailure)):
            session = self.session()
            with self.subTest(precondition=precondition), self.assertRaises(expected):
                session.wait_until(lambda: False, 3, precondition=precondition)
            self.assertEqual(session.frame, 3)

    def test_wait_parameters_are_validated(self):
        session = self.session()
        with self.assertRaises(UsageError):
            session.wait_until(lambda: True, 0)
        with self.assertRaises(UsageError):
            session.wait_until(lambda: True, 1, stable=0)


if __name__ == "__main__":
    unittest.main()
