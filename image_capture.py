import logging
import os
import subprocess
from pathlib import Path


_FFMPEG_DECODE_ERROR_MARKERS = (
    "error while decoding",
    "concealing",
    "corrupt",
    "invalid data found",
    "bytestream",
    "missing picture",
    "no frame",
)


def capture_still_image(stream_url: str, image_path: str | os.PathLike[str], *, retries: int = 2) -> Path:
    """Capture a still image from the RTSP stream, retrying on decode corruption."""
    output_path = Path(image_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            "-i",
            stream_url,
            "-vf",
            r"select=eq(pict_type\,I)",
            "-fps_mode",
            "passthrough",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-update",
            "1",
            "-y",
            str(output_path),
        ]

        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=4,
            )
        except subprocess.CalledProcessError as exc:
            last_error = exc
            logging.warning(
                "Still image capture failed on attempt %d/%d: %s",
                attempt,
                retries,
                exc.stderr.strip() or exc,
            )
            continue
        except subprocess.TimeoutExpired as exc:
            last_error = exc
            logging.warning(
                "Still image capture timed out on attempt %d/%d after %.1fs.",
                attempt,
                retries,
                float(exc.timeout) if exc.timeout is not None else 0.0,
            )
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                logging.warning("Failed to remove timed out capture at %s", output_path, exc_info=True)
            continue

        stderr = result.stderr.lower()
        if any(marker in stderr for marker in _FFMPEG_DECODE_ERROR_MARKERS):
            last_error = RuntimeError(result.stderr.strip() or "ffmpeg reported decode corruption")
            logging.warning(
                "Still image capture reported decode corruption on attempt %d/%d: %s",
                attempt,
                retries,
                result.stderr.strip(),
            )
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                logging.warning("Failed to remove corrupted capture at %s", output_path, exc_info=True)
            continue

        if not output_path.exists() or output_path.stat().st_size == 0:
            last_error = RuntimeError("ffmpeg reported success but did not produce a valid image file")
            logging.warning(
                "Still image capture produced no data on attempt %d/%d.",
                attempt,
                retries,
            )
            continue

        return output_path

    if last_error is None:
        last_error = RuntimeError("still image capture failed")
    raise last_error
