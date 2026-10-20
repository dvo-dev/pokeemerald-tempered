from fractions import Fraction
import json
import os
from pathlib import Path
import select
import subprocess
import time

from .artifacts import sha256
from .errors import QAError, UsageError


WIDTH = 240
HEIGHT = 160
FRAME_BYTES = WIDTH * HEIGHT * 4


def verify_media(metadata: dict, *, frames: int, fps: Fraction, scale: int) -> dict:
    streams = metadata.get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        raise QAError("Recording must contain exactly one video stream and no audio")
    stream = streams[0]
    expected = {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
                "width": WIDTH * scale, "height": HEIGHT * scale}
    if any(stream.get(key) != value for key, value in expected.items()):
        raise QAError("Recording codec, pixel format, or dimensions are incorrect")
    try:
        count = int(stream["nb_read_frames"])
        rate = Fraction(stream["r_frame_rate"])
        average = Fraction(stream["avg_frame_rate"])
        duration = Fraction(stream["duration"])
        tick = Fraction(stream["time_base"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise QAError(f"Incomplete ffprobe frame/rate/duration metadata: {error}") from error
    if frames < 1 or count != frames:
        raise QAError(f"Recording frame-count mismatch: {count} != {frames}")
    if rate != fps or average != fps:
        raise QAError(f"Recording frame-rate mismatch: {rate}/{average} != {fps}")
    if tick <= 0 or abs(duration - frames / fps) > max(tick, Fraction(1, 1_000_000)):
        raise QAError("Recording duration does not match its captured frame schedule")
    return {"verified": True, "frames": frames, "fps": str(fps),
            "width": WIDTH * scale, "height": HEIGHT * scale,
            "duration": str(duration), "codec": "h264", "pixel_format": "yuv420p"}


class Recorder:
    def __init__(self, directory: Path, *, start_frame: int, fps: Fraction,
                 scale: int = 4, interval: int = 1):
        if scale < 1 or interval < 1 or max(WIDTH * scale, HEIGHT * scale) >= 2**31:
            raise UsageError("Scale/interval must be positive and dimensions fit signed 32-bit")
        if fps <= 0:
            raise QAError("Emulator frame rate must be positive")
        self.directory = directory
        self.scale = scale
        self.interval = interval
        self.fps = fps / interval
        self.start_frame = start_frame
        self.seen = 0
        self.frames = 0
        self.finished = False
        self.partial = directory / "video.partial.mp4"
        self.output = directory / "video.mp4"
        self.log_path = directory / "ffmpeg.log"
        self.log = self.log_path.open("xb")
        self.command = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-n",
            "-f", "rawvideo", "-pixel_format", "rgb0", "-video_size", "240x160",
            "-framerate", str(self.fps), "-i", "pipe:0", "-an",
            "-vf", f"scale=iw*{scale}:ih*{scale}:flags=neighbor",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
            "-movflags", "+faststart", str(self.partial),
        ]
        try:
            self.process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=self.log, bufsize=0,
            )
        except OSError as error:
            self.log.close()
            raise QAError(f"Cannot start ffmpeg: {error}") from error
        if self.process.stdin is None:
            self.abort()
            raise QAError("ffmpeg stdin was not created")
        os.set_blocking(self.process.stdin.fileno(), False)

    def append(self, frame: bytes, frame_index: int) -> None:
        if self.finished or frame_index != self.start_frame + self.seen + 1:
            raise QAError("Recording frame order is inconsistent")
        if len(frame) != FRAME_BYTES:
            raise QAError(f"Expected {FRAME_BYTES} RGBX bytes, got {len(frame)}")
        capture = self.seen % self.interval == 0
        self.seen += 1
        if not capture:
            return
        pipe = self.process.stdin
        if pipe is None or pipe.closed:
            raise QAError("Recording input is closed")
        remaining = memoryview(frame)
        deadline = time.monotonic() + 30
        while remaining:
            delay = deadline - time.monotonic()
            if delay <= 0 or not select.select([], [pipe], [], delay)[1]:
                raise QAError(f"ffmpeg input timed out; see {self.log_path}")
            try:
                count = os.write(pipe.fileno(), remaining)
            except BlockingIOError:
                continue
            except BrokenPipeError as error:
                raise QAError(f"ffmpeg stopped accepting frames; see {self.log_path}") from error
            if count < 1:
                raise QAError(f"Short ffmpeg pipe write; see {self.log_path}")
            remaining = remaining[count:]
        self.frames += 1

    def abort(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.close()
        self.finished = True

    def finish(self) -> dict:
        if self.finished:
            raise QAError("Recording has already been finalized or aborted")
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                code = self.process.wait(timeout=30)
            except subprocess.TimeoutExpired as error:
                raise QAError(f"ffmpeg finalization timed out; see {self.log_path}") from error
            if code != 0:
                raise QAError(f"ffmpeg exited {code}; see {self.log_path}")
            probe_command = [
                "ffprobe", "-v", "error", "-count_frames", "-show_streams",
                "-show_format", "-of", "json", str(self.partial),
            ]
            try:
                probe = subprocess.run(probe_command, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise QAError(f"Cannot verify recording with ffprobe: {error}") from error
            if probe.returncode != 0:
                raise QAError(f"ffprobe failed ({probe.returncode}): {probe.stderr.strip()}")
            try:
                metadata = json.loads(probe.stdout)
            except json.JSONDecodeError as error:
                raise QAError(f"ffprobe returned invalid JSON: {error}") from error
            if not isinstance(metadata, dict):
                raise QAError("ffprobe did not return an object")
            report = verify_media(
                metadata, frames=self.frames, fps=self.fps, scale=self.scale
            )
            os.link(self.partial, self.output)
            self.partial.unlink()
            report.update({"file": self.output.name, "sha256": sha256(self.output),
                           "command": self.command, "probe_command": probe_command,
                           "interval": self.interval, "emulated_frames": self.seen,
                           "first_frame": self.start_frame + 1})
            self.finished = True
            return report
        finally:
            if not self.finished:
                self.abort()
            else:
                self.log.close()


def screenshots(path: Path, frame: bytes) -> list[dict]:
    from PIL import Image

    if len(frame) != FRAME_BYTES:
        raise QAError("Screenshot requires a complete native RGBX framebuffer")
    native = Image.frombytes("RGBX", (WIDTH, HEIGHT), frame).convert("RGB")
    enlarged = native.resize((WIDTH * 4, HEIGHT * 4), resample=Image.Resampling.NEAREST)
    results = []
    for output, picture in (
        (path, native), (path.with_name(path.stem + "@4x.png"), enlarged),
    ):
        with output.open("xb") as stream:
            picture.save(stream, format="PNG")
        results.append({"file": str(output), "sha256": sha256(output),
                        "width": picture.width, "height": picture.height})
    return results
