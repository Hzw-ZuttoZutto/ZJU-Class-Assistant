from __future__ import annotations

import unittest

from src.cli.parser import build_parser


class MicCliTests(unittest.TestCase):
    def test_mic_listen_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["mic-listen"])
        self.assertEqual(args.command, "mic-listen")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 18765)
        self.assertEqual(args.session_dir, "")
        self.assertEqual(args.mic_upload_token, "")
        self.assertEqual(args.mic_chunk_max_bytes, 10 * 1024 * 1024)
        self.assertEqual(args.mic_chunk_dir, "_rt_chunks_mic")
        self.assertEqual(args.rt_model, "gpt-4.1-mini")
        self.assertEqual(args.rt_stt_model, "whisper-large-v3")
        self.assertEqual(args.rt_stt_request_timeout_sec, 8.0)
        self.assertEqual(args.rt_analysis_request_timeout_sec, 15.0)
        self.assertFalse(args.rt_dingtalk_enabled)
        self.assertEqual(args.rt_dingtalk_cooldown_sec, 30.0)
        self.assertFalse(args.rt_profile_enabled)

    def test_mic_publish_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "mic-publish",
                "--target-url",
                "http://127.0.0.1:18765",
                "--mic-upload-token",
                "token",
                "--device",
                "Microphone (Realtek(R) Audio)",
            ]
        )
        self.assertEqual(args.command, "mic-publish")
        self.assertEqual(args.target_url, "http://127.0.0.1:18765")
        self.assertEqual(args.mic_upload_token, "token")
        self.assertEqual(args.device, "Microphone (Realtek(R) Audio)")
        self.assertEqual(args.chunk_seconds, 10.0)
        self.assertEqual(args.request_timeout_sec, 10.0)
        self.assertEqual(args.retry_base_sec, 0.5)
        self.assertEqual(args.retry_max_sec, 8.0)

    def test_mic_listen_profile_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["mic-listen", "--rt-profile-enabled"])
        self.assertTrue(args.rt_profile_enabled)

    def test_mic_listen_dingtalk_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["mic-listen", "--rt-dingtalk-enabled", "--rt-dingtalk-cooldown-sec", "45"]
        )
        self.assertTrue(args.rt_dingtalk_enabled)
        self.assertEqual(args.rt_dingtalk_cooldown_sec, 45.0)

    def test_mic_list_devices_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["mic-list-devices", "--ffmpeg-bin", "C:/ffmpeg/bin/ffmpeg.exe"])
        self.assertEqual(args.command, "mic-list-devices")
        self.assertEqual(args.ffmpeg_bin, "C:/ffmpeg/bin/ffmpeg.exe")

    def test_mic_decimal_chunk_seconds(self) -> None:
        parser = build_parser()
        listen_args = parser.parse_args(["mic-listen", "--rt-chunk-seconds", "17.5"])
        publish_args = parser.parse_args(
            [
                "mic-publish",
                "--target-url",
                "http://127.0.0.1:18765",
                "--mic-upload-token",
                "token",
                "--device",
                "Microphone (Realtek(R) Audio)",
                "--chunk-seconds",
                "17.5",
            ]
        )
        self.assertEqual(listen_args.rt_chunk_seconds, 17.5)
        self.assertEqual(publish_args.chunk_seconds, 17.5)


if __name__ == "__main__":
    unittest.main()
