from fractions import Fraction
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.qa.qa.errors import QAError, UsageError
from tools.qa.qa.record import FRAME_BYTES, Recorder, verify_media


FPS = Fraction(262144, 4389)


def metadata(frames=20, fps=FPS, scale=4):
    return {"streams": [{
        "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
        "width": 240 * scale, "height": 160 * scale, "nb_read_frames": str(frames),
        "r_frame_rate": str(fps), "avg_frame_rate": str(fps),
        "time_base": "1/262144", "duration": str(float(frames / fps)),
    }]}


class RecordTests(unittest.TestCase):
    def test_exact_media_shape_rate_and_frame_count_pass(self):
        report = verify_media(metadata(), frames=20, fps=FPS, scale=4)
        self.assertTrue(report["verified"])
        self.assertEqual((report["width"], report["height"]), (960, 640))
        self.assertEqual(report["fps"], "262144/4389")

    def test_decimated_rate_is_exact_rational(self):
        report = verify_media(metadata(5, FPS / 4), frames=5, fps=FPS / 4, scale=4)
        self.assertEqual(report["fps"], str(FPS / 4))

    def test_wrong_count_rate_dimensions_or_codec_fail(self):
        for field, wrong in (
            ("nb_read_frames", "19"), ("r_frame_rate", "60"),
            ("avg_frame_rate", "60"), ("width", 240),
            ("pix_fmt", "yuv444p"), ("codec_name", "mpeg4"),
        ):
            value = metadata()
            value["streams"][0][field] = wrong
            with self.subTest(field=field), self.assertRaises(QAError):
                verify_media(value, frames=20, fps=FPS, scale=4)

    def test_audio_or_multiple_streams_fail(self):
        value = metadata()
        value["streams"].append({"codec_type": "audio"})
        with self.assertRaisesRegex(QAError, "no audio"):
            verify_media(value, frames=20, fps=FPS, scale=4)

    def test_duration_mismatch_and_invalid_metadata_fail(self):
        for field, wrong in (("duration", "99"), ("nb_read_frames", "N/A"),
                             ("time_base", "0/1"), ("r_frame_rate", "0/0")):
            value = metadata()
            value["streams"][0][field] = wrong
            with self.subTest(field=field), self.assertRaises(QAError):
                verify_media(value, frames=20, fps=FPS, scale=4)

    def test_invalid_scale_interval_or_rate_never_starts_encoder(self):
        with tempfile.TemporaryDirectory() as directory:
            for overrides in ({"scale": 0}, {"interval": 0}, {"fps": Fraction(0)}):
                values = dict(start_frame=0, fps=FPS, **overrides) if "fps" not in overrides else {
                    "start_frame": 0, **overrides,
                }
                with patch("subprocess.Popen") as process:
                    with self.assertRaises((UsageError, QAError)):
                        Recorder(Path(directory), **values)
                    process.assert_not_called()

    def recorder(self, interval=1):
        recorder = object.__new__(Recorder)
        recorder.start_frame = 600
        recorder.interval = interval
        recorder.seen = 0
        recorder.frames = 0
        recorder.finished = False
        recorder.log_path = Path("ffmpeg.log")
        recorder.process = Mock()
        recorder.process.stdin.closed = False
        recorder.process.stdin.fileno.return_value = 99
        return recorder

    def test_every_frame_is_counted_and_decimation_schedule_matches(self):
        recorder = self.recorder(interval=3)
        frame = b"\x00" * FRAME_BYTES
        with patch("select.select", return_value=([], [recorder.process.stdin], [])):
            with patch("os.write", side_effect=lambda fd, value: len(value)) as write:
                for index in range(601, 608):
                    recorder.append(frame, index)
        self.assertEqual((recorder.seen, recorder.frames), (7, 3))
        self.assertEqual(write.call_count, 3)

    def test_missing_or_out_of_order_frame_is_error(self):
        recorder = self.recorder()
        with self.assertRaisesRegex(QAError, "frame order"):
            recorder.append(b"\x00" * FRAME_BYTES, 602)

    def test_broken_pipe_is_explicit_harness_error(self):
        recorder = self.recorder()
        with patch("select.select", return_value=([], [recorder.process.stdin], [])):
            with patch("os.write", side_effect=BrokenPipeError):
                with self.assertRaisesRegex(QAError, "stopped accepting"):
                    recorder.append(b"\x00" * FRAME_BYTES, 601)

    def test_write_timeout_is_explicit_harness_error(self):
        recorder = self.recorder()
        with patch("select.select", return_value=([], [], [])):
            with self.assertRaisesRegex(QAError, "timed out"):
                recorder.append(b"\x00" * FRAME_BYTES, 601)

    def test_abort_terminates_only_owned_process(self):
        recorder = self.recorder()
        recorder.log = io.BytesIO()
        recorder.process.poll.return_value = None
        recorder.process.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 5), -9]
        recorder.abort()
        recorder.process.terminate.assert_called_once()
        recorder.process.kill.assert_called_once()
        self.assertTrue(recorder.finished)
        self.assertTrue(recorder.log.closed)


if __name__ == "__main__":
    unittest.main()
