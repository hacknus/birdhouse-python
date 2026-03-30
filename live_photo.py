from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import subprocess


@dataclass
class LivePhotoResult:
    still_path: Path | None
    motion_path: Path | None
    used_heic: bool
    warning: str | None = None


def _run_ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def save_live_photo_bundle(
    rtsp_url: str,
    timestamp: str,
    output_dir: str = "gallery/live_photos",
    duration_seconds: float = 5.0,
) -> LivePhotoResult:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    jpg_path = out_dir / f"{timestamp}.jpg"
    heic_path = out_dir / f"{timestamp}.heic"
    mov_path = out_dir / f"{timestamp}.mov"

    _run_ffmpeg([
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-q:v", "2",
        "-y",
        str(jpg_path),
    ])

    used_heic = False
    warning = None
    still_path = jpg_path

    try:
        _run_ffmpeg([
            "ffmpeg",
            "-i", str(jpg_path),
            "-frames:v", "1",
            "-c:v", "libx265",
            "-tag:v", "hvc1",
            "-f", "heic",
            "-y",
            str(heic_path),
        ])
        used_heic = True
        still_path = heic_path
        try:
            jpg_path.unlink(missing_ok=True)
        except OSError:
            logging.warning("Failed to remove temporary jpg %s", jpg_path, exc_info=True)
    except subprocess.CalledProcessError as exc:
        warning = "HEIC conversion failed; saved JPG instead."
        logging.warning("HEIC conversion failed: %s", exc.stderr.strip())

    _run_ffmpeg([
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-t", str(duration_seconds),
        "-an",
        "-c:v", "libx264",
        "-movflags", "+faststart",
        "-y",
        str(mov_path),
    ])

    return LivePhotoResult(
        still_path=still_path,
        motion_path=mov_path,
        used_heic=used_heic,
        warning=warning,
    )
