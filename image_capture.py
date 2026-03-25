import os
import subprocess
import tempfile
import time
from pathlib import Path


def capture_still_image(
    stream_url: str,
    image_path: str | os.PathLike[str],
    *,
    warmup_seconds: float = 5.0,
    timeout_seconds: float = 9.0,
) -> Path:
    """
    Grab a still image by sampling the RTSP stream for a few seconds and keeping the latest frame.

    This keeps idle CPU near zero and behaves more like a browser player: allow the stream to settle,
    then use the newest decoded frame instead of the very first one after connect.
    """
    output_path = Path(image_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=output_path.parent) as temp_dir:
        frame_pattern = str(Path(temp_dir) / "frame-%03d.jpg")
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-i",
            stream_url,
            "-q:v",
            "2",
            "-y",
            frame_pattern,
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        capture_deadline = time.monotonic() + warmup_seconds
        hard_deadline = time.monotonic() + timeout_seconds
        selected_frame: Path | None = None

        try:
            while time.monotonic() < hard_deadline:
                frames = sorted(Path(temp_dir).glob("frame-*.jpg"))
                if frames and time.monotonic() >= capture_deadline:
                    selected_frame = frames[-1]
                    break

                if process.poll() is not None:
                    break
                time.sleep(0.2)

            if selected_frame is None:
                frames = sorted(Path(temp_dir).glob("frame-*.jpg"))
                if frames:
                    selected_frame = frames[-1]

            if selected_frame is None:
                raise RuntimeError("ffmpeg did not produce any snapshot frames")

            selected_frame.replace(output_path)
            return output_path
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
