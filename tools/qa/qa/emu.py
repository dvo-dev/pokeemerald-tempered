from contextlib import ExitStack
from fractions import Fraction
import hashlib
from pathlib import Path
import time

from .build import Bundle
from .checkpoint import (
    MAX_CORE_BYTES, MAX_EPOCH_MS, MIN_EPOCH_MS, SAVE_BYTES,
    read_checkpoint, write_checkpoint,
)
from .errors import BehaviorFailure, PreconditionFailure, QAError, UsageError
from .memory import Memory, Symbols
from .record import HEIGHT, WIDTH
from .runtime import Runtime, guard_native_process, verify_loaded_paths


KEYS = {"A": 1, "B": 2, "SELECT": 4, "START": 8, "RIGHT": 16, "LEFT": 32,
        "UP": 64, "DOWN": 128, "R": 256, "L": 512}


def key_mask(chord: str) -> int:
    names = [part.strip().upper() for part in chord.split("+")]
    if not names or any(name not in KEYS for name in names):
        raise UsageError(f"Unknown key/chord {chord!r}; use {' '.join(KEYS)} joined with +")
    mask = 0
    for name in names:
        mask |= KEYS[name]
    return mask


class NativeFile:
    def __init__(self, session: "Session", data: bytes = b""):
        if session.closed or session.core is None:
            raise QAError("Cannot allocate a native VFile for a closed session")
        self.session = session
        self.pointer = session.lib.VFileMemChunk(
            data if data else session.ffi.NULL, len(data)
        )
        if self.pointer == session.ffi.NULL:
            raise QAError("Cannot allocate a native memory VFile")

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if self.pointer is not None:
            pointer, self.pointer = self.pointer, None
            closed = pointer.close(pointer)
            if kind is None and not closed:
                raise QAError("Native memory VFile close failed")

    def transfer(self, function):
        if self.pointer is None:
            raise QAError("Native memory VFile ownership was already transferred")
        if not function(self.session.native, self.pointer):
            raise QAError("Cannot attach the in-memory save")
        pointer, self.pointer = self.pointer, None
        return pointer

    def bytes(self) -> bytes:
        pointer = self.pointer
        if pointer is None:
            raise QAError("Native memory VFile ownership was transferred")
        size = int(pointer.size(pointer))
        if not 0 < size <= MAX_CORE_BYTES or pointer.seek(pointer, 0, 0) != 0:
            raise QAError("Invalid native checkpoint size or seek")
        data = self.session.ffi.new("unsigned char[]", size)
        if pointer.read(pointer, data, size) != size:
            raise QAError("Native checkpoint read was incomplete")
        return bytes(self.session.ffi.buffer(data, size))


class Session:
    def __init__(self, bundle: Bundle, runtime: Runtime, run, epoch_ms: int,
                 initial_save: bytes | None = None):
        self.bundle = bundle
        self.runtime = runtime
        self.run = run
        self.epoch_ms = epoch_ms
        self.core = None
        self.symbols = None
        self.closed = False
        self.recorder = None
        self.last_frame = None
        self.frame_callbacks = 0
        self.key_reads = 0
        self.crashes = []
        self.parent_checkpoint = None
        if (not isinstance(epoch_ms, int) or isinstance(epoch_ms, bool)
                or not MIN_EPOCH_MS <= epoch_ms < MAX_EPOCH_MS):
            raise UsageError("Epoch must be between 2000 and 2099 UTC")
        initial_save = initial_save if initial_save is not None else b"\xff" * SAVE_BYTES
        if len(initial_save) != SAVE_BYTES:
            raise UsageError("Save input must be exactly 128 KiB without an RTC trailer")
        with ExitStack() as rollback:
            rollback.callback(self.close)
            guard_native_process()
            time.tzset()
            if time.timezone != 0 or time.daylight != 0:
                raise QAError("QA native execution requires the UTC timezone")
            from mgba import core, image
            import mgba
            import mgba._pylib as pylib

            verify_loaded_paths(runtime, pylib.__file__)
            self.ffi, self.lib = pylib.ffi, pylib.lib
            self.core = core.load_path(str(bundle.file("rom")))
            if self.core is None or self.core.platform != self.lib.mPLATFORM_GBA:
                raise QAError("The bundle did not load as a GBA ROM")
            self.native = self.core._core
            self.gba = self.core._native
            self.native.opts.useBios = False
            self.native.opts.skipBios = False
            self.native.rtc.override = self.lib.RTC_FIXED
            self.native.rtc.value = epoch_ms
            self.image = image.Image(WIDTH, HEIGHT)
            if (self.ffi.sizeof("mColor") != 4 or self.image.stride < WIDTH
                    or self.ffi.sizeof(self.image.buffer) < self.image.stride * HEIGHT * 4):
                raise QAError("Expected a complete 32-bit native RGBX framebuffer")
            self.core.set_video_buffer(self.image)
            self.core.add_frame_callback(self._on_frame)
            self.core._callbacks.keys_read.append(self._on_keys)
            self.core._callbacks.core_crashed.append(
                lambda: self.crashes.append("mGBA reported a core crash")
            )
            self.core.reset()
            self.core.set_keys(raw=0)
            if self.frame != 0 or self.frame_callbacks != 0:
                raise QAError("Reset unexpectedly advanced an emulator frame")
            if self.gba.memory.savedata.realVf != self.ffi.NULL:
                raise QAError("An unexpected initial save was autoloaded")
            with NativeFile(self, b"\xff" * SAVE_BYTES) as anchor:
                self.anchor = anchor.transfer(self.native.loadSave)
            with NativeFile(self, initial_save) as temporary:
                temporary.transfer(self.native.loadTemporarySave)
            self._guard()
            if self.flash() != initial_save:
                raise QAError("Loaded temporary save bytes do not match the input")
            self.rom_size = bundle.file("rom").stat().st_size
            self.regions = []
            for region, base, expected, field in (
                (2, 0x02000000, 0x40000, "wram"), (3, 0x03000000, 0x8000, "iwram"),
                (8, 0x08000000, self.rom_size, "rom"),
            ):
                size = self.ffi.new("size_t *")
                pointer = self.native.getMemoryBlock(self.native, region, size)
                if (pointer == self.ffi.NULL or int(size[0]) != expected
                        or pointer != getattr(self.gba.memory, field)):
                    raise QAError(f"Invalid backing memory block: {region}")
                self.regions.append((base, expected, self.ffi.cast("uint8_t *", pointer)))
            self.symbols = Symbols(bundle.file("elf"))
            self.memory = Memory(self.symbols, self._read, self.rom_size)
            if self.memory.bytes_at(0x080000AC, 4) != b"BPEE":
                raise QAError("Emulator ROM game code does not match Emerald")
            self.fps = Fraction(self.core.frequency, self.core.frame_cycles)
            if self.fps <= 0 or self.core.desired_video_dimensions() != (WIDTH, HEIGHT):
                raise QAError("Invalid GBA video dimensions or hardware timing")
            run.manifest.update({
                "runtime": runtime.identity,
                "environment": runtime.metadata["environment"],
                "core": {
                    "version": mgba.__version__, "bios": "hle",
                    "use_bios": bool(self.native.opts.useBios),
                    "skip_bios": bool(self.native.opts.skipBios),
                    "vba_bug_compat": bool(self.gba.vbaBugCompat),
                    "idle_optimization": int(self.gba.idleOptimization),
                    "fps": str(self.fps), "save_bytes": SAVE_BYTES,
                    "rtc_mode": "fixed", "epoch_ms": epoch_ms, "timezone": "UTC",
                    "input_policy": "polled-minimum-2-frames-press-and-release",
                    "native_logging": "verbatim native C default; no Python logger",
                },
                "bundle_files": bundle.manifest["identity"]["files"],
                "initial_save_sha256": hashlib.sha256(initial_save).hexdigest(),
            })
            run.flush()
            rollback.pop_all()

    def _on_frame(self):
        self.frame_callbacks += 1

    def _on_keys(self):
        self.key_reads += 1

    @property
    def frame(self) -> int:
        if self.closed or self.core is None:
            raise QAError("Emulator session is not open")
        return int(self.core.frame_counter)

    def _guard(self) -> None:
        if self.closed or self.core is None:
            raise QAError("Emulator session is not open")
        if self.crashes:
            raise QAError(self.crashes[0])
        save = self.gba.memory.savedata
        if (save.type != self.lib.GBA_SAVEDATA_FLASH1M or save.data == self.ffi.NULL
                or save.currentBank == self.ffi.NULL or save.realVf != self.anchor
                or save.vf == self.ffi.NULL or save.vf == save.realVf
                or save.mapMode != self.lib.MAP_READ or save.maskWriteback):
            raise QAError("Private read-only FLASH1M mask invariant was lost")
        if (self.native.rtc.override != self.lib.RTC_FIXED
                or self.native.rtc.value != self.epoch_ms
                or self.gba.memory.hw.rtc.offset != 0
                or not self.gba.memory.hw.devices & self.lib.HW_RTC
                or self.gba.rtcSource != self.ffi.addressof(self.native.rtc, "d")):
            raise QAError("Fixed hardware RTC invariant was lost")
        if self.native.opts.useBios or self.native.opts.skipBios:
            raise QAError("HLE BIOS options changed")

    def _read(self, address: int, size: int) -> bytes:
        if self.closed:
            raise QAError("Emulator session is closed")
        for base, length, pointer in self.regions:
            offset = address - base
            if 0 <= offset and 0 <= size <= length - offset:
                return bytes(self.ffi.buffer(pointer + offset, size))
        raise QAError(f"Unsafe emulator read: 0x{address:08x}+{size}")

    def flash(self) -> bytes:
        if self.closed or self.core is None:
            raise QAError("Emulator session is not open")
        result = self.ffi.new("void **")
        size = int(self.native.savedataClone(self.native, result))
        try:
            if size != SAVE_BYTES or result[0] == self.ffi.NULL:
                raise QAError("Cannot clone exactly 128 KiB of savedata")
            return bytes(self.ffi.buffer(result[0], size))
        finally:
            if result[0] != self.ffi.NULL:
                self.lib.free(result[0])

    def advance(self, frames: int = 1) -> None:
        if frames < 0:
            raise UsageError("Frame count cannot be negative")
        for _ in range(frames):
            self._guard()
            before, callbacks = self.frame, self.frame_callbacks
            self.core.run_frame()
            after = self.frame
            self.run.manifest["current_frame"] = after
            self.run.manifest["frames_advanced"] += 1
            if after != before + 1 or self.frame_callbacks != callbacks + 1:
                raise QAError("Frame advancement or callback count is inconsistent")
            self._guard()
            raw = bytes(self.ffi.buffer(
                self.image.buffer, self.image.stride * HEIGHT * 4
            ))
            self.last_frame = b"".join(
                raw[row * self.image.stride * 4:
                    row * self.image.stride * 4 + WIDTH * 4]
                for row in range(HEIGHT)
            )
            if self.recorder is not None:
                self.recorder.append(self.last_frame, after)

    def wait_until(self, predicate, max_frames: int, *, precondition: bool = False,
                   stable: int = 2, description: str = "RAM predicate") -> None:
        if max_frames < 1 or stable < 1:
            raise UsageError("Wait deadline and stability count must be positive")
        consecutive = 0
        for _ in range(max_frames):
            self.advance()
            consecutive = consecutive + 1 if predicate() else 0
            if consecutive >= stable:
                return
        error = PreconditionFailure if precondition else BehaviorFailure
        raise error(f"{description} was not reached within {max_frames} frames")

    def tap(self, chord: str) -> None:
        mask = key_mask(chord)
        for phase, value in (("press", mask), ("release", 0)):
            before_reads = self.key_reads
            self.core.set_keys(raw=value)
            self.run.manifest["inputs"].append({"frame": self.frame, "keys": value})
            self.run.event("keys", phase=phase, chord=chord, keys=value)
            for count in range(1, 121):
                self.advance()
                raw = self.memory.read("main.c::gMain.heldKeysRaw")
                if count >= 2 and self.key_reads > before_reads and raw == value:
                    break
            else:
                raise BehaviorFailure(f"Game did not poll the {phase} of {chord}")
        if self.native.getKeys(self.native) != 0:
            raise QAError("Tap left emulator keys pressed")

    def rgbx(self) -> bytes:
        if self.last_frame is None:
            raise QAError("Advance a real frame before capturing a post-load framebuffer")
        return self.last_frame

    def controller(self) -> dict:
        self._guard()
        save = self.gba.memory.savedata
        offset = int(self.ffi.cast("uintptr_t", save.currentBank)) - int(
            self.ffi.cast("uintptr_t", save.data)
        )
        if offset not in (0, 0x10000) or save.vf.size(save.vf) != SAVE_BYTES:
            raise QAError("Flash bank or active memory save size is invalid")
        timing = self.ffi.addressof(self.gba, "timing")
        dust = self.ffi.addressof(save, "dust")
        scheduled = bool(self.lib.mTimingIsScheduled(timing, dust))
        return {
            **{name: int(getattr(save, name)) for name in (
                "type", "command", "flashState", "settling", "readBitsRemaining",
                "readAddress", "writeAddress", "mapMode",
            )},
            "bank_offset": offset, "dust_scheduled": scheduled,
            "dust_relative_when": int(self.lib.mTimingUntil(timing, dust)) if scheduled else None,
            "mask_writeback": bool(save.maskWriteback), "temporary_mask": save.vf != save.realVf,
        }

    def observe(self) -> dict:
        self._guard()
        raw = self.last_frame
        rgb_hash = None
        if raw is not None:
            rgb = bytearray(WIDTH * HEIGHT * 3)
            for channel in range(3):
                rgb[channel::3] = raw[channel::4]
            rgb_hash = hashlib.sha256(rgb).hexdigest()
        rtc = self.gba.memory.hw.rtc
        return {
            "frame": self.frame, "keys": int(self.native.getKeys(self.native)),
            "game_keys_raw": self.memory.read("main.c::gMain.heldKeysRaw"),
            "ewram_sha256": hashlib.sha256(self._read(0x02000000, 0x40000)).hexdigest(),
            "iwram_sha256": hashlib.sha256(self._read(0x03000000, 0x8000)).hexdigest(),
            "rng_hex": self.memory.bytes("random.c::gRngValue").hex(),
            "flash_sha256": hashlib.sha256(self.flash()).hexdigest(),
            "rgb_sha256": rgb_hash,
            "controller": self.controller(),
            "rtc": {
                "override": int(self.native.rtc.override), "value_ms": int(self.native.rtc.value),
                "offset": int(rtc.offset),
                "time_bcd": bytes(self.ffi.buffer(rtc.time, 7)).hex(),
                "control": int(rtc.control),
                "protocol": {name: int(getattr(rtc, name)) for name in (
                    "bytesRemaining", "bitsRead", "bits", "commandActive",
                    "sckEdge", "sioOutput", "command",
                )},
            },
        }

    def save(self, path: Path):
        self.rgbx()
        observation = self.observe()
        if observation["keys"] != 0 or observation["game_keys_raw"] != 0:
            raise QAError("Checkpoint requires both emulator and game keys to be released")
        with NativeFile(self) as output:
            if not self.lib.mCoreSaveStateNamed(self.native, output.pointer, 8):
                raise QAError("Native RTC-only state serialization failed")
            state = output.bytes()
        result = write_checkpoint(
            path, bundle_id=self.bundle.bundle_id, runtime=self.runtime.identity,
            frame=self.frame, epoch_ms=self.epoch_ms,
            core_state=state, savedata=self.flash(),
            parent_sha256=self.parent_checkpoint, observations=observation,
        )
        self.run.event("checkpoint", file=str(path), sha256=result.sha256)
        return result

    def restore(self, path: Path, *, expected_sha256: str | None = None) -> None:
        self._guard()
        checkpoint = read_checkpoint(
            path, bundle_id=self.bundle.bundle_id, runtime=self.runtime.identity
        )
        if expected_sha256 is not None and checkpoint.sha256 != expected_sha256:
            raise QAError("Checkpoint changed between preflight and execution")
        with NativeFile(self, checkpoint.savedata) as savedata:
            savedata.transfer(self.native.loadTemporarySave)
        before = self.frame_callbacks
        with NativeFile(self, checkpoint.core_state) as state:
            if not self.lib.mCoreLoadStateNamed(self.native, state.pointer, 8):
                raise QAError("Native RTC-only state restoration failed")
        self.epoch_ms = checkpoint.manifest["epoch_ms"]
        self.core.set_keys(raw=0)
        self.last_frame = None
        self._guard()
        if self.frame_callbacks != before or self.frame != checkpoint.manifest["frame"]:
            raise QAError("State restoration advanced a hidden frame or lost the frame counter")
        actual = self.observe()
        expected = checkpoint.manifest["observations"]
        for name in ("frame", "keys", "game_keys_raw", "ewram_sha256", "iwram_sha256",
                     "rng_hex", "flash_sha256", "controller", "rtc"):
            if actual.get(name) != expected.get(name):
                raise QAError(f"Checkpoint restore mismatch: {name}")
        self.parent_checkpoint = checkpoint.sha256
        self.run.manifest.update(initial_frame=self.frame, current_frame=self.frame)
        self.run.manifest["core"]["epoch_ms"] = self.epoch_ms
        self.run.event("restore", sha256=checkpoint.sha256)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            if self.core is not None:
                self.ffi.release(self.core._core)
                self.core = None
        finally:
            if self.symbols is not None:
                self.symbols.close()
