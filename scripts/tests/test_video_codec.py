import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pipeline


class VideoCodecTests(unittest.TestCase):
    def test_github_actions_always_uses_cpu_encoder(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False), mock.patch.object(
            pipeline.subprocess, "run"
        ) as probe:
            codec = pipeline.video_codec()

        self.assertEqual(codec, ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"])
        probe.assert_not_called()

    def test_local_nvenc_remains_available(self):
        probe_result = mock.Mock(stdout=" V..... h264_nvenc NVIDIA NVENC H.264 encoder")
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}, clear=False), mock.patch.object(
            pipeline.subprocess, "run", return_value=probe_result
        ):
            codec = pipeline.video_codec()

        self.assertEqual(codec[1], "h264_nvenc")


if __name__ == "__main__":
    unittest.main()
