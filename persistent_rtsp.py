from __future__ import annotations

from pathlib import Path
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from live_photo import LivePhotoResult, _write_still_metadata, save_live_photo_bundle


class PersistentRtspRecorder:
    def __init__(
        self,
        rtsp_url: str,
        *,
        buffer_dir: str = "gallery/.rtsp_buffer",
        rolling_window_seconds: int = 12,
        segment_time_seconds: float = 1.0,
        default_duration_seconds: float = 5.0,
        post_trigger_seconds: float = 2.5,
    ) -> None:
        self.rtsp_url = rtsp_url
        self.buffer_dir = Path(buffer_dir)
        self.rolling_window_seconds = rolling_window_seconds
        self.segment_time_seconds = segment_time_seconds
        self.default_duration_seconds = default_duration_seconds
        self.post_trigger_seconds = post_trigger_seconds
        self.initial_wait_timeout_seconds = max(
            3.0,
            self.segment_time_seconds * 3,
        )

        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()
        self._export_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        with self._process_lock:
            if self._started and self._process is not None and self._process.poll() is None:
                return
            self._stop_event.clear()
            self._start_process_locked()
            self._started = True
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None
        with self._process_lock:
            self._stop_process_locked()
            self._started = False

    def ensure_running(self) -> None:
        with self._process_lock:
            if self._process is None or self._process.poll() is not None:
                self._start_process_locked()

    def export_live_photo(
        self,
        timestamp: str,
        *,
        output_dir: str = "gallery",
        duration_seconds: float | None = None,
        post_trigger_seconds: float | None = None,
    ) -> LivePhotoResult:
        duration_seconds = duration_seconds or self.default_duration_seconds
        post_trigger_seconds = (
            self.post_trigger_seconds if post_trigger_seconds is None else post_trigger_seconds
        )
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        with self._export_lock:
            self.ensure_running()
            if post_trigger_seconds > 0:
                time.sleep(post_trigger_seconds)

            segments = self._select_recent_segments(duration_seconds=duration_seconds)
            if not segments:
                segments = self._wait_for_segments(duration_seconds=duration_seconds)
            if not segments:
                logging.warning("RTSP buffer not ready; falling back to direct live capture.")
                live_photo = save_live_photo_bundle(
                    rtsp_url=self.rtsp_url,
                    timestamp=timestamp,
                    output_dir=output_dir,
                    duration_seconds=duration_seconds,
                )
                warning = "Persistent RTSP buffer was not ready; used direct capture fallback"
                if live_photo.warning:
                    live_photo.warning = f"{live_photo.warning}; {warning}"
                else:
                    live_photo.warning = warning
                return live_photo

            mov_path = out_dir / f"{timestamp}.mov"
            jpg_path = out_dir / f"{timestamp}.jpg"
            asset_id = str(uuid.uuid4()).upper()
            warning_parts: list[str] = []

            try:
                self._render_segments_to_mov(
                    segments=segments,
                    output_path=mov_path,
                    asset_id=asset_id,
                    duration_seconds=duration_seconds,
                )
            except subprocess.TimeoutExpired:
                logging.error(
                    "Timed out while rendering %d RTSP segments into %s.",
                    len(segments),
                    mov_path,
                )
                raise

            try:
                self._extract_still_from_clip(
                    clip_path=mov_path,
                    still_path=jpg_path,
                    seek_seconds=max(0.0, duration_seconds / 2.0),
                )
            except subprocess.TimeoutExpired:
                logging.error("Timed out while extracting still image from %s.", mov_path)
                raise

            try:
                still_metadata_written = _write_still_metadata(jpg_path, asset_id)
            except subprocess.CalledProcessError as exc:
                logging.warning("Failed to write Apple still metadata: %s", exc.stderr.strip())
                still_metadata_written = False
                warning_parts.append("Failed to write Apple still metadata")
            except subprocess.TimeoutExpired:
                logging.warning("Timed out while writing Apple still metadata.")
                still_metadata_written = False
                warning_parts.append("Timed out while writing Apple still metadata")

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
                bundle_id=timestamp,
                asset_id=asset_id,
                used_heic=False,
                apple_metadata_ready=False,
                warning="; ".join(warning_parts) if warning_parts else None,
            )

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(2.0):
            try:
                self.ensure_running()
                with self._export_lock:
                    self._prune_old_segments()
            except Exception:
                logging.exception("Persistent RTSP recorder monitor failure.")

    def _start_process_locked(self) -> None:
        self._stop_process_locked()
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        segment_pattern = str(self.buffer_dir / "segment_%Y%m%d_%H%M%S.ts")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-timeout", "10000000",
            "-i", self.rtsp_url,
            "-map", "0:v:0",
            "-an",
            "-c", "copy",
            "-f", "segment",
            "-segment_time", str(self.segment_time_seconds),
            "-strftime", "1",
            "-segment_format", "mpegts",
            "-segment_format_options", "mpegts_flags=resend_headers",
            segment_pattern,
        ]
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        logging.info("Started persistent RTSP recorder with PID %s", self._process.pid)

    def _stop_process_locked(self) -> None:
        if self._process is None:
            return
        proc = self._process
        self._process = None
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def _prune_old_segments(self) -> None:
        keep_after = time.time() - max(self.rolling_window_seconds, self.default_duration_seconds) - 5
        for path in self.buffer_dir.glob("segment_*.ts"):
            try:
                if path.stat().st_mtime < keep_after:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                continue

    def _select_recent_segments(self, *, duration_seconds: float) -> list[Path]:
        self._prune_old_segments()
        required_segments = max(2, int(duration_seconds / self.segment_time_seconds) + 2)
        segments = sorted(self.buffer_dir.glob("segment_*.ts"), key=lambda p: p.stat().st_mtime)
        return segments[-required_segments:]

    def _wait_for_segments(self, *, duration_seconds: float) -> list[Path]:
        deadline = time.time() + self.initial_wait_timeout_seconds
        while time.time() < deadline:
            segments = self._select_recent_segments(duration_seconds=duration_seconds)
            if segments:
                return segments
            self.ensure_running()
            time.sleep(0.25)
        return []

    def _render_segments_to_mov(
        self,
        *,
        segments: list[Path],
        output_path: Path,
        asset_id: str,
        duration_seconds: float,
    ) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
            concat_path = Path(tmp.name)
            for segment in segments:
                tmp.write(f"file '{segment.resolve()}'\n")
        temp_ts_path = output_path.with_suffix(".buffer.ts")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "warning",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_path),
                    "-c", "copy",
                    "-y",
                    str(temp_ts_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=max(30, int(duration_seconds * 4), len(segments) * 4),
            )

            total_span_seconds = len(segments) * self.segment_time_seconds
            clip_start_seconds = max(0.0, total_span_seconds - duration_seconds)

            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "warning",
                    "-ss", str(clip_start_seconds),
                    "-i", str(temp_ts_path),
                    "-t", str(duration_seconds),
                    "-an",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-pix_fmt", "yuv420p",
                    "-b:v", "1500k",
                    "-maxrate", "1800k",
                    "-bufsize", "3000k",
                    "-movflags", "+faststart+use_metadata_tags",
                    "-metadata", f"com.apple.quicktime.content.identifier={asset_id}",
                    "-y",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=max(45, int(duration_seconds * 8)),
            )
        finally:
            concat_path.unlink(missing_ok=True)
            temp_ts_path.unlink(missing_ok=True)

    def _extract_still_from_clip(self, *, clip_path: Path, still_path: Path, seek_seconds: float) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-ss", str(seek_seconds),
                "-i", str(clip_path),
                "-frames:v", "1",
                "-q:v", "2",
                "-y",
                str(still_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
