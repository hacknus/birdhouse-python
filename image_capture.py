import os
import subprocess
import tempfile
from pathlib import Path


def capture_still_image(
    stream_url: str,
    image_path: str | os.PathLike[str],
    *,
    warmup_seconds: int = 5,
    capture_fps: int = 1,
    timeout_seconds: int = 8,
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
            "-t",
            str(warmup_seconds),
            "-vf",
            f"fps={capture_fps}",
            "-q:v",
            "2",
            "-y",
            frame_pattern,
        ]

        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

        frames = sorted(Path(temp_dir).glob("frame-*.jpg"))
        if not frames:
            stderr = result.stderr.strip()
            raise RuntimeError(stderr or "ffmpeg did not produce any snapshot frames")

        latest_frame = frames[-1]
        latest_frame.replace(output_path)
        return output_path
