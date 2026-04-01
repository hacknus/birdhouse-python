from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import logging
import subprocess
import shutil
import threading
import uuid


_CAPTURE_LOCK = threading.Lock()
_FFMPEG_TIMEOUT_SECONDS = 20
_RTSP_IO_TIMEOUT_US = "10000000"


@dataclass
class LivePhotoResult:
    still_path: Path | None
    motion_path: Path | None
    asset_id: str
    used_heic: bool
    apple_metadata_ready: bool
    warning: str | None = None


def _run_ffmpeg(cmd: list[str], timeout_seconds: int = _FFMPEG_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _has_exiftool() -> bool:
    return shutil.which("exiftool") is not None


def _write_still_metadata(still_path: Path, asset_id: str) -> bool:
    if not _has_exiftool():
        return False

    subprocess.run(
        [
            "exiftool",
            "-overwrite_original",
            f"-ContentIdentifier={asset_id}",
            str(still_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return True


def save_live_photo_bundle(
    rtsp_url: str,
    timestamp: str,
    output_dir: str = "gallery/live_photos",
    duration_seconds: float = 5.0,
) -> LivePhotoResult:
    with _CAPTURE_LOCK:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        asset_id = str(uuid.uuid4()).upper()
        jpg_path = out_dir / f"{timestamp}.jpg"
        mov_path = out_dir / f"{timestamp}.mov"

        _run_ffmpeg([
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-rw_timeout", _RTSP_IO_TIMEOUT_US,
            "-i", rtsp_url,
            "-frames:v", "1",
            "-q:v", "2",
            "-y",
            str(jpg_path),
        ], timeout_seconds=10)

        warning_parts: list[str] = []
        still_metadata_written = False

        try:
            still_metadata_written = _write_still_metadata(jpg_path, asset_id)
        except subprocess.CalledProcessError as exc:
            logging.warning("Failed to write Apple still metadata: %s", exc.stderr.strip())
            warning_parts.append("Failed to write Apple still metadata")
        except subprocess.TimeoutExpired:
            logging.warning("Timed out while writing Apple still metadata.")
            warning_parts.append("Timed out while writing Apple still metadata")

        _run_ffmpeg([
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-rw_timeout", _RTSP_IO_TIMEOUT_US,
            "-i", rtsp_url,
            "-map", "0:v:0",
            "-t", str(duration_seconds),
            "-an",
            "-c", "copy",
            "-movflags", "+faststart+use_metadata_tags",
            "-metadata", f"com.apple.quicktime.content.identifier={asset_id}",
            "-y",
            str(mov_path),
        ], timeout_seconds=max(15, int(duration_seconds) + 10))

        if still_metadata_written:
            warning_parts.append(
                "Only partial Apple metadata was written; MOV still-image-time metadata is still missing"
            )
        else:
            warning_parts.append(
                "Full Apple Live Photo metadata is incomplete on this device; exiftool is not installed"
            )

        return LivePhotoResult(
            still_path=jpg_path,
            motion_path=mov_path,
            asset_id=asset_id,
            used_heic=False,
            apple_metadata_ready=False,
            warning="; ".join(warning_parts) if warning_parts else None,
        )
