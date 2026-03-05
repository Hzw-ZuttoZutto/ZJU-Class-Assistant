from __future__ import annotations

import json
import signal
import subprocess
import tempfile
from pathlib import Path
from shutil import which


class FfmpegBackend:
    def __init__(self) -> None:
        self.ffmpeg = which("ffmpeg") or ""
        self.ffprobe = which("ffprobe") or ""

    def ensure_available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def probe_av(self, url: str, timeout_sec: float = 5.0) -> tuple[bool, bool]:
        if not url:
            return False, False

        cmd = [
            self.ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            url,
        ]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1.0, timeout_sec),
            )
        except subprocess.SubprocessError:
            return False, False

        if proc.returncode != 0:
            return False, False

        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return False, False

        streams = payload.get("streams")
        if not isinstance(streams, list):
            return False, False

        has_video = any(isinstance(item, dict) and item.get("codec_type") == "video" for item in streams)
        has_audio = any(isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams)
        return has_audio, has_video

    def start_capture(self, stream_url: str, output_path: Path) -> subprocess.Popen:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-y",
            "-rw_timeout",
            "10000000",
            "-fflags",
            "+discardcorrupt",
            "-i",
            stream_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "mpegts",
            str(output_path),
        ]
        return subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop_capture(self, proc: subprocess.Popen, grace_sec: float = 3.0) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=max(0.5, grace_sec))
        except Exception:
            proc.kill()
            proc.wait(timeout=1.0)

    def render_gap_clip(self, duration_sec: float, output_path: Path) -> bool:
        if duration_sec <= 0.0:
            return False
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1280x720:r=25",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t",
            f"{duration_sec:.3f}",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-f",
            "mpegts",
            str(output_path),
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        return proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0

    def finalize_segment(
        self,
        input_parts: list[Path],
        output_mp4: Path,
        *,
        prefer_copy: bool,
    ) -> bool:
        if not input_parts:
            return False
        output_mp4.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as handle:
            concat_file = Path(handle.name)
            for part in input_parts:
                escaped = str(part).replace("'", "'\\''")
                handle.write(f"file '{escaped}'\n")

        try:
            if prefer_copy and self._concat_copy(concat_file, output_mp4):
                return True
            return self._concat_transcode(concat_file, output_mp4)
        finally:
            try:
                concat_file.unlink()
            except OSError:
                pass

    def export_mp3(self, input_mp4: Path, output_mp3: Path) -> bool:
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_mp4),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(output_mp3),
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        return proc.returncode == 0 and output_mp3.exists() and output_mp3.stat().st_size > 0

    def _concat_copy(self, concat_file: Path, output_mp4: Path) -> bool:
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_mp4),
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        return proc.returncode == 0 and output_mp4.exists() and output_mp4.stat().st_size > 0

    def _concat_transcode(self, concat_file: Path, output_mp4: Path) -> bool:
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-fflags",
            "+genpts",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_mp4),
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        return proc.returncode == 0 and output_mp4.exists() and output_mp4.stat().st_size > 0
