import logging
import os
import subprocess
import threading
import time
from pathlib import Path


_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"
_MAX_BUFFER_BYTES = 8 * 1024 * 1024

_reader_lock = threading.Lock()
_reader = None


class StreamSnapshotReader:
    def __init__(self, stream_url: str, *, reconnect_delay_s: float = 1.0) -> None:
        self.stream_url = stream_url
        self.reconnect_delay_s = reconnect_delay_s

        self._latest_frame: bytes | None = None
        self._latest_frame_monotonic = 0.0
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, name="stream-snapshot-reader", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._process_lock:
            if self._process is not None and self._process.poll() is None:
                try:
                    self._process.terminate()
                except Exception:
                    logging.debug("[snapshot] Failed to terminate ffmpeg during shutdown.", exc_info=True)
        self._thread.join(timeout=2.0)

    def get_frame(self, *, wait_timeout_s: float = 10.0, max_age_s: float = 5.0) -> bytes:
        deadline = time.monotonic() + wait_timeout_s
        with self._condition:
            while not self._stop_event.is_set():
                if self._latest_frame is not None:
                    age_s = time.monotonic() - self._latest_frame_monotonic
                    if age_s <= max_age_s:
                        return self._latest_frame

                remaining_s = deadline - time.monotonic()
                if remaining_s <= 0:
                    break
                self._condition.wait(timeout=min(remaining_s, 0.5))

        raise TimeoutError(
            f"Timed out after {wait_timeout_s:.1f}s waiting for a fresh frame from {self.stream_url}"
        )

    def _publish_frame(self, frame_bytes: bytes) -> None:
        if len(frame_bytes) < 1024:
            return

        with self._condition:
            self._latest_frame = frame_bytes
            self._latest_frame_monotonic = time.monotonic()
            self._condition.notify_all()

    def _read_stderr(self, stderr_pipe) -> None:
        try:
            for raw_line in iter(stderr_pipe.readline, b""):
                if self._stop_event.is_set():
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    logging.debug("[snapshot] ffmpeg: %s", line)
        except Exception:
            logging.warning("[snapshot] stderr reader failed.", exc_info=True)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            process = None
            try:
                command = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-threads",
                    "1",
                    "-rtsp_transport",
                    "tcp",
                    "-skip_frame",
                    "nokey",
                    "-i",
                    self.stream_url,
                    "-an",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "mjpeg",
                    "-q:v",
                    "5",
                    "-",
                ]
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                with self._process_lock:
                    self._process = process
                logging.info("[snapshot] Connected background reader to %s", self.stream_url)

                stderr_thread = threading.Thread(
                    target=self._read_stderr,
                    args=(process.stderr,),
                    name="stream-snapshot-stderr",
                    daemon=True,
                )
                stderr_thread.start()

                buffer = bytearray()
                stdout_pipe = process.stdout
                assert stdout_pipe is not None

                while not self._stop_event.is_set():
                    chunk = stdout_pipe.read(65536)
                    if not chunk:
                        break

                    buffer.extend(chunk)
                    if len(buffer) > _MAX_BUFFER_BYTES:
                        del buffer[:-_MAX_BUFFER_BYTES]

                    while True:
                        start = buffer.find(_JPEG_SOI)
                        if start < 0:
                            if len(buffer) > 2:
                                del buffer[:-2]
                            break

                        if start > 0:
                            del buffer[:start]

                        end = buffer.find(_JPEG_EOI, 2)
                        if end < 0:
                            break

                        frame_bytes = bytes(buffer[:end + 2])
                        del buffer[:end + 2]
                        self._publish_frame(frame_bytes)

                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2.0)
            except Exception:
                logging.warning("[snapshot] Background reader failed; reconnecting.", exc_info=True)
                if process is not None:
                    try:
                        process.kill()
                    except Exception:
                        pass
            finally:
                with self._process_lock:
                    if self._process is process:
                        self._process = None

            if not self._stop_event.wait(self.reconnect_delay_s):
                logging.info("[snapshot] Reconnecting background reader to %s", self.stream_url)


def ensure_stream_snapshot_reader(stream_url: str) -> StreamSnapshotReader:
    global _reader

    with _reader_lock:
        if _reader is None or _reader.stream_url != stream_url:
            if _reader is not None:
                _reader.stop()
            _reader = StreamSnapshotReader(stream_url)
            _reader.start()
        return _reader


def stop_stream_snapshot_reader() -> None:
    global _reader

    with _reader_lock:
        if _reader is not None:
            _reader.stop()
            _reader = None


def capture_still_image(
    stream_url: str,
    image_path: str | os.PathLike[str],
    *,
    wait_timeout_s: float = 10.0,
    max_age_s: float = 5.0,
) -> Path:
    reader = ensure_stream_snapshot_reader(stream_url)
    frame_bytes = reader.get_frame(wait_timeout_s=wait_timeout_s, max_age_s=max_age_s)

    output_path = Path(image_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_bytes(frame_bytes)
    temp_path.replace(output_path)
    return output_path
