from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from tools.qa.qa.flows import smoke


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            "main.c::gMain.callback2": 0x08000101,
            "palette.c::gPaletteFade.active": 0,
            "script.c::sLockFieldControls": 0,
            "script.c::sGlobalScriptContextStatus": 2,
            "load_save.c::gSaveBlock1Ptr": 0x02001000,
            "load_save.c::gSaveBlock1Ptr->location.mapGroup": 0,
            "load_save.c::gSaveBlock1Ptr->location.mapNum": 9,
            "start_menu.c::gMenuCallback": 0,
        }
        self.oracles = object.__new__(smoke.Oracles)
        self.oracles.memory = SimpleNamespace(read=lambda name: self.values[name])
        self.oracles.overworld = 0x08000100
        self.oracles.truck = 0x08000200
        self.oracles.menu = 0x08000300
        self.oracles.title = 0x08000400
        self.oracles.truck_map = (25, 40)
        self.oracles.task = Mock(return_value=None)

    def test_unlocked_other_map_reports_general_field_readiness(self):
        self.assertTrue(self.oracles.field_ready())
        self.assertFalse(self.oracles.truck_ready())
        observation = self.oracles.observe()
        self.assertEqual(observation["location"], {"group": 0, "number": 9})
        self.assertTrue(observation["ready"]["field"])

    def test_truck_readiness_adds_the_map_requirement(self):
        self.values["load_save.c::gSaveBlock1Ptr->location.mapGroup"] = 25
        self.values["load_save.c::gSaveBlock1Ptr->location.mapNum"] = 40
        self.assertTrue(self.oracles.field_ready())
        self.assertTrue(self.oracles.truck_ready())

    def test_callback_fade_and_script_control_guards_still_apply(self):
        for name, value in (
            ("main.c::gMain.callback2", 0),
            ("palette.c::gPaletteFade.active", 1),
            ("script.c::sLockFieldControls", 1),
            ("script.c::sGlobalScriptContextStatus", 0),
        ):
            with self.subTest(name=name):
                original = self.values[name]
                self.values[name] = value
                self.assertFalse(self.oracles.field_ready())
                self.assertFalse(self.oracles.truck_ready())
                self.values[name] = original

    def test_active_menu_or_truck_sequence_blocks_field_readiness(self):
        for function in (self.oracles.menu, self.oracles.truck):
            with self.subTest(function=function):
                self.oracles.task.side_effect = lambda value: 0 if value == function else None
                self.assertFalse(self.oracles.field_ready())
                self.assertFalse(self.oracles.truck_ready())

    def test_smoke_setup_and_return_still_require_the_truck(self):
        session = SimpleNamespace(wait_until=Mock(), tap=Mock())
        run = SimpleNamespace(case=lambda name: nullcontext({}))
        with patch.object(smoke, "Oracles", return_value=self.oracles):
            with patch.object(smoke, "capture", return_value={}):
                smoke.execute(session, run)
        self.assertEqual(session.wait_until.call_args_list, [
            call(self.oracles.title_ready, 6000, precondition=True,
                 description="ready title screen"),
            call(self.oracles.truck_ready, 3000, description="controllable truck overworld"),
            call(self.oracles.menu_ready, 300, description="ready Start menu"),
            call(self.oracles.truck_ready, 300, description="menu closed and field restored"),
        ])
        self.assertEqual(session.tap.call_args_list, [call("SELECT"), call("START"), call("B")])


if __name__ == "__main__":
    unittest.main()
