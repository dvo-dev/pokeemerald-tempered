import re

from ..errors import QAError
from ..record import screenshots


CASES = [
    {"name": "title-ready", "precondition": "fresh non-release Emerald boot",
     "event": "wait", "expected": "ready title task and no palette fade", "max_frames": 6000},
    {"name": "quickstart-truck", "precondition": "ready title screen",
     "event": "SELECT", "expected": "controllable truck overworld", "max_frames": 3000},
    {"name": "start-menu-open", "precondition": "controllable truck overworld",
     "event": "START", "expected": "ready Start menu input task", "max_frames": 300},
    {"name": "start-menu-close", "precondition": "ready Start menu",
     "event": "B", "expected": "menu torn down and field control restored", "max_frames": 300},
]
CONTROL_CASES = [
    {"name": "negative-control", "precondition": "declared by control flow",
     "event": "deliberately wrong oracle", "expected": "nonzero exit", "max_frames": 6000},
]
CONTROLS = {"negative-wrong-callback", "negative-unreachable", "negative-unknown-symbol"}


def map_id(header: str, name: str) -> tuple[int, int]:
    matches = re.findall(
        r"^\s*" + re.escape(name)
        + r"\s*=\s*\(\s*(\d+)\s*\|\s*\(\s*(\d+)\s*<<\s*8\s*\)\s*\)\s*,",
        header, re.MULTILINE,
    )
    if len(matches) != 1:
        raise QAError(f"Missing or ambiguous generated map identifier: {name}")
    number, group = map(int, matches[0])
    if not 0 <= number < 256 or not 0 <= group < 256:
        raise QAError(f"Invalid generated map identifier: {name}")
    return group, number


class Oracles:
    def __init__(self, session):
        self.memory = session.memory
        self.task_count = self.memory.array_length("task.c::gTasks")
        self.title = self.memory.function("title_screen.c::Task_TitleScreenPhase3")
        self.truck = self.memory.function("field_special_scene.c::Task_HandleTruckSequence")
        self.menu = self.memory.function("start_menu.c::Task_ShowStartMenu")
        self.menu_input = self.memory.function("start_menu.c::HandleStartMenuInput")
        self.overworld = self.memory.function("overworld.c::CB2_Overworld")
        self.truck_map = map_id(
            session.bundle.file("constants").read_text(), "MAP_INSIDE_OF_TRUCK"
        )

    def task(self, function: int) -> int | None:
        found = [
            index for index in range(self.task_count)
            if self.memory.read(f"task.c::gTasks[{index}].isActive")
            and self.memory.read(f"task.c::gTasks[{index}].func") & ~1 == function
        ]
        if len(found) > 1:
            raise QAError("Multiple active tasks match the readiness oracle")
        return found[0] if found else None

    def title_ready(self) -> bool:
        return self.task(self.title) is not None and not self.memory.read(
            "palette.c::gPaletteFade.active"
        )

    def field_ready(self) -> bool:
        memory = self.memory
        if not (
            memory.read("main.c::gMain.callback2") & ~1 == self.overworld
            and not memory.read("palette.c::gPaletteFade.active")
            and not memory.read("script.c::sLockFieldControls")
            and memory.read("script.c::sGlobalScriptContextStatus") != 0
            and self.task(self.truck) is None
            and self.task(self.menu) is None
        ):
            return False
        group = memory.read("load_save.c::gSaveBlock1Ptr->location.mapGroup")
        number = memory.read("load_save.c::gSaveBlock1Ptr->location.mapNum")
        return (group, number) == self.truck_map

    def menu_ready(self) -> bool:
        task = self.task(self.menu)
        return (
            task is not None
            and self.memory.read(f"task.c::gTasks[{task}].data[0]") == 1
            and self.memory.read("start_menu.c::gMenuCallback") & ~1 == self.menu_input
            and not self.memory.read("palette.c::gPaletteFade.active")
        )

    def observe(self) -> dict:
        memory = self.memory
        menu = self.task(self.menu)
        pointer = memory.read("load_save.c::gSaveBlock1Ptr")
        location = None
        if pointer != 0:
            location = {
                "group": memory.read("load_save.c::gSaveBlock1Ptr->location.mapGroup"),
                "number": memory.read("load_save.c::gSaveBlock1Ptr->location.mapNum"),
            }
        return {
            "callback2": memory.read("main.c::gMain.callback2"),
            "menu_callback": memory.read("start_menu.c::gMenuCallback"),
            "menu_task_index": menu,
            "menu_task_state": memory.read(f"task.c::gTasks[{menu}].data[0]")
            if menu is not None else None,
            "field_control_lock": memory.read("script.c::sLockFieldControls"),
            "script_context_status": memory.read("script.c::sGlobalScriptContextStatus"),
            "palette_fade_active": memory.read("palette.c::gPaletteFade.active"),
            "location": location,
            "ready": {
                "title": self.title_ready(), "menu": self.menu_ready(),
                "field": self.field_ready(),
            },
        }


def capture(session, run, name: str) -> dict:
    observation = session.observe()
    run.manifest["screenshots"].extend(
        screenshots(run.directory / (name + ".png"), session.rgbx())
    )
    run.event("checkpoint-observation", name=name, observation=observation)
    return observation


def execute(session, run, flow: str = "smoke") -> None:
    if flow == "negative-unknown-symbol":
        with run.case("negative-control"):
            session.advance()
            session.memory.read("rtc.c::QA_DeliberatelyMissingSymbol")
        return
    if flow == "negative-unreachable":
        with run.case("negative-control"):
            session.wait_until(
                lambda: False, 2, precondition=True, description="deliberately unreachable setup"
            )
        return
    oracles = Oracles(session)
    if flow == "negative-wrong-callback":
        with run.case("negative-control"):
            session.wait_until(oracles.title_ready, 6000, precondition=True)
            session.wait_until(
                lambda: session.memory.read("main.c::gMain.callback2") & ~1 == oracles.overworld,
                2, description="deliberately wrong title-screen callback",
            )
        return
    if flow != "smoke":
        raise QAError(f"Unknown flow: {flow}")
    with run.case("title-ready") as case:
        session.wait_until(
            oracles.title_ready, 6000, precondition=True, description="ready title screen"
        )
        case["observed"] = capture(session, run, "title")
    with run.case("quickstart-truck") as case:
        session.tap("SELECT")
        session.wait_until(oracles.field_ready, 3000, description="controllable truck overworld")
        case["observed"] = capture(session, run, "truck")
    with run.case("start-menu-open") as case:
        session.tap("START")
        session.wait_until(oracles.menu_ready, 300, description="ready Start menu")
        case["observed"] = capture(session, run, "menu-open")
    with run.case("start-menu-close") as case:
        session.tap("B")
        session.wait_until(oracles.field_ready, 300, description="menu closed and field restored")
        case["observed"] = capture(session, run, "menu-closed")
