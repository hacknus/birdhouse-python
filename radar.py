#!/usr/bin/env python3
# Copyright (c) Acconeer AB, 2025
# All rights reserved

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
import datetime
from typing import Optional
import logging
import csv
import subprocess
from unibe_mail import Reporter
from encoding import encode_email

import acconeer.exptool as et
from acconeer.exptool import a121
from acconeer.exptool.a121.algo.breathing._processor import Processor, ProcessorConfig
from acconeer.exptool.a121.algo.breathing._ref_app import (
    BreathingProcessorConfig,
    RefAppConfig,
    get_sensor_config,
)
from acconeer.exptool.a121.algo.presence import ProcessorConfig as PresenceProcessorConfig
from dotenv import dotenv_values
import psycopg

from camera import get_ir_led_state, turn_ir_on, turn_ir_off
from ignore_motion import are_we_still_blocked
from image_upload import UploadImageError, upload_live_photo
from persistent_rtsp import PersistentRtspRecorder
from postgresql_store import PostgresTimeSeriesStore
from time_utils import bern_image_timestamp


@dataclass
class Sample:
    timestamp_s: float
    activity: float
    temperature: float
    app_state: str
    presence_detected: bool
    presence_distance_m: float
    intra_presence_score: float
    inter_presence_score: float
    breathing_rate_bpm: Optional[float]


class Radar:
    def __init__(
            self,
            *,
            ip_address: str = "localhost",
            tcp_port: int = 6110,
            sensor_id: int = 1,
            frame_rate: float = 30.0,
            sweeps_per_frame: int = 16,
            hwaas: int = 32,
            start_m: float = 0.12,
            end_m: float = 0.33,
            lowest_bpm: float = 40.0,
            highest_bpm: float = 300.0,
            time_series_s: float = 8.0,
            num_distances: int = 1,
            distance_det_s: float = 6.0,
            write_period_s: float = 2.0,
            motion_activity_threshold: float = 6.0,
            env_file: str = ".env",
            recorder: PersistentRtspRecorder | None = None,
    ) -> None:
        # Track last image save time and last email sent time
        self.last_image_time = 0

        env_values = dotenv_values(env_file)
        self.mediamtx_url = env_values['IMAGE_GRAB_URL']
        self.db_store = PostgresTimeSeriesStore(env_values)
        self.bucket = self.db_store.bucket
        self.upload_image_token = env_values['UPLOAD_IMAGE_TOKEN']
        self.upload_image_url = env_values['UPLOAD_IMAGE_URL']
        self.rtsp_recorder = recorder or PersistentRtspRecorder(self.mediamtx_url)
        self._owns_rtsp_recorder = recorder is None
        self.rtsp_recorder.start()

        # replace this with custom email-interface
        self.email_reporter = Reporter("Voegeli")

        self.ip_address = ip_address
        self.tcp_port = tcp_port
        self.sensor_id = sensor_id
        self.frame_rate = frame_rate
        self.sweeps_per_frame = sweeps_per_frame
        self.hwaas = hwaas
        self.start_m = start_m
        self.end_m = end_m
        self.lowest_bpm = lowest_bpm
        self.highest_bpm = highest_bpm
        self.time_series_s = time_series_s
        self.num_distances = num_distances
        self.distance_det_s = distance_det_s
        self.write_period_s = write_period_s
        self.motion_activity_threshold = motion_activity_threshold
        # Only consider presence within the configured range
        self.min_presence_distance_m = start_m
        self.max_presence_distance_m = end_m

        self._client: Optional[a121.Client] = None
        self._processor: Optional[Processor] = None
        self._latest_lock = threading.Lock()
        self._latest: Optional[Sample] = None
        self._stop_event = threading.Event()
        self._presence_event = threading.Event()
        self._sampler_thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._presence_thread: Optional[threading.Thread] = None
        self._motion_active_prev: Optional[bool] = None
        self._motion_low_since_s: Optional[float] = None
        self._last_debug_log_s = 0.0
        self._accum_lock = threading.Lock()
        self._accum = {
            "count": 0,
            "activity_sum": 0.0,
            "temp_sum": 0.0,
            "distance_sum": 0.0,
            "distance_count": 0,
            "bpm_sum": 0.0,
            "bpm_count": 0,
            "presence_any": False,
        }
        self._reconnect_delay_s = 2.0

    def _create_sensor_config(self) -> tuple[a121.SensorConfig, ProcessorConfig]:
        breathing_config = BreathingProcessorConfig(
            lowest_breathing_rate=self.lowest_bpm,
            highest_breathing_rate=self.highest_bpm,
            time_series_length_s=self.time_series_s,
        )

        presence_config = PresenceProcessorConfig(
            intra_detection_threshold=6.0,
            intra_frame_time_const=0.15,
            inter_frame_fast_cutoff=20.0,
            inter_frame_slow_cutoff=0.2,
            inter_frame_deviation_time_const=0.5,
        )

        ref_app_config = RefAppConfig(
            use_presence_processor=True,
            num_distances_to_analyze=self.num_distances,
            distance_determination_duration=self.distance_det_s,
            start_m=self.start_m,
            end_m=self.end_m,
            hwaas=self.hwaas,
            frame_rate=self.frame_rate,
            sweeps_per_frame=self.sweeps_per_frame,
            breathing_config=breathing_config,
            presence_config=presence_config,
        )

        sensor_config = get_sensor_config(ref_app_config=ref_app_config)
        processor_config = ProcessorConfig()
        processor_config.use_presence_processor = ref_app_config.use_presence_processor
        processor_config.num_distances_to_analyze = ref_app_config.num_distances_to_analyze
        processor_config.distance_determination_duration = (
            ref_app_config.distance_determination_duration
        )
        processor_config.breathing_config = ref_app_config.breathing_config
        processor_config.presence_config = ref_app_config.presence_config
        return sensor_config, processor_config

    def _connect_radar(self) -> None:
        sensor_config, processor_config = self._create_sensor_config()
        self._client = a121.Client.open(ip_address=self.ip_address, tcp_port=self.tcp_port)
        metadata = self._client.setup_session(a121.SessionConfig({self.sensor_id: sensor_config}))
        assert not isinstance(metadata, list)
        self._processor = Processor(
            sensor_config=sensor_config, processor_config=processor_config, metadata=metadata
        )
        self._client.start_session()

    def _disconnect_radar(self) -> None:
        if self._client is not None:
            try:
                self._client.stop_session()
            except Exception:
                pass
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._processor = None
        self._motion_active_prev = None
        self._motion_low_since_s = None

    def run(self) -> None:
        et.utils.config_logging()
        try:
            self._connect_radar()
        except Exception as e:
            logging.warning("[radar] initial connect failed, retrying in sampler: %s", e)
            self._disconnect_radar()

        self._sampler_thread = threading.Thread(
            target=self._sampler, name="sampler", daemon=True
        )
        self._writer_thread = threading.Thread(
            target=self._writer, name="writer", daemon=True
        )
        self._presence_thread = threading.Thread(
            target=self._presence_watcher, name="presence_watcher", daemon=True
        )
        self._sampler_thread.start()
        self._writer_thread.start()
        self._presence_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._presence_event.set()
        if self._sampler_thread is not None:
            self._sampler_thread.join(timeout=2.0)
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=2.0)
        if self._presence_thread is not None:
            self._presence_thread.join(timeout=2.0)
        self._disconnect_radar()
        if self._owns_rtsp_recorder:
            self.rtsp_recorder.stop()
        self.db_store.close()

    def _sampler(self) -> None:
        while not self._stop_event.is_set():
            if self._client is None or self._processor is None:
                try:
                    self._connect_radar()
                    logging.info("[radar] reconnected")
                except Exception as e:
                    logging.warning("[radar] reconnect failed: %s", e)
                    self._disconnect_radar()
                    if self._stop_event.wait(self._reconnect_delay_s):
                        break
                    continue

            try:
                result = self._client.get_next()
                processor_result = self._processor.process(result)
            except Exception as e:
                logging.warning("[radar] sampler error, reconnecting: %s", e)
                self._disconnect_radar()
                if self._stop_event.wait(self._reconnect_delay_s):
                    break
                continue

            presence = processor_result.presence_result
            presence_distance = presence.presence_distance
            activity = max(presence.intra_presence_score, presence.inter_presence_score)
            distance_ok = self.min_presence_distance_m < presence_distance < self.max_presence_distance_m
            presence_valid = presence.presence_detected and distance_ok
            motion_active = presence_valid and activity >= self.motion_activity_threshold
            now_s = time.time()

            # Trigger only on rising edge of presence (low->high),
            # and only if presence was low for a full 60s.
            presence_active = presence_valid
            if self._motion_active_prev is not None and presence_active and not self._motion_active_prev:
                low_duration_s = (
                    now_s - self._motion_low_since_s
                    if self._motion_low_since_s is not None
                    else 0.0
                )
                if self._motion_low_since_s is not None and low_duration_s >= 60.0:
                    logging.info(
                        "[radar] motion rising edge detected at distance=%.3fm activity=%.3f",
                        presence_distance,
                        activity,
                    )
                    self._presence_event.set()
                else:
                    logging.info(
                        "[radar] rising edge ignored; presence-low duration %.1fs < 60.0s",
                        low_duration_s,
                    )

            if presence_active:
                self._motion_low_since_s = None
            elif self._motion_low_since_s is None:
                self._motion_low_since_s = now_s
            self._motion_active_prev = presence_active

            temperature_c = result.temperature

            breathing_rate = None
            if processor_result.breathing_result is not None:
                breathing_rate = processor_result.breathing_result.breathing_rate
            if not presence_valid:
                breathing_rate = None

            sample = Sample(
                timestamp_s=time.time(),
                activity=activity,
                temperature=temperature_c,
                app_state=processor_result.app_state.name,
                presence_detected=presence.presence_detected,
                presence_distance_m=presence.presence_distance,
                intra_presence_score=presence.intra_presence_score,
                inter_presence_score=presence.inter_presence_score,
                breathing_rate_bpm=breathing_rate,
            )
            with self._latest_lock:
                self._latest = sample
            with self._accum_lock:
                self._accum["count"] += 1
                self._accum["activity_sum"] += activity
                self._accum["temp_sum"] += temperature_c
                if presence_valid:
                    self._accum["distance_sum"] += presence_distance
                    self._accum["distance_count"] += 1
                if breathing_rate is not None:
                    self._accum["bpm_sum"] += breathing_rate
                    self._accum["bpm_count"] += 1
                if presence_valid:
                    self._accum["presence_any"] = True

    def write_device_data_to_db(self, device_data, measurement=None):
        self.db_store.write_device_data(device_data, measurement=measurement)

        # Function to store sensor data in the database

    def motion_detected_callback(self, _os=None):

        # Check if motion detection should be ignored
        if are_we_still_blocked():
            print("Motion detection temporarily ignored.")
            return
        if get_ir_led_state():
            print("Motion detection ignored because IR LED is on.")
            return

        current_time = time.time()

        # Send an email only if at least 23 hours have passed since last successful send
        email_throttle_seconds = 23 * 60 * 60
        file_path = "last_email_sent.txt"
        last_email_time = 0

        # Read existing timestamp
        try:
            with open(file_path, "r") as f:
                content = f.readline().strip()
                last_email_time = int(content) if content else 0
        except (FileNotFoundError, ValueError):
            last_email_time = 0

        if current_time - last_email_time >= email_throttle_seconds:
            sent_count = 0
            try:
                csv_file = 'newsletter_subscribers.csv'
                with open(csv_file, mode='r') as file:
                    reader = csv.reader(file)
                    subscribers = list(reader)
                    for subscriber in subscribers:
                        if not subscriber:
                            continue
                        email = subscriber[0].strip()
                        if not email:
                            continue

                        encoded_email = encode_email(email)
                        base_url = "https://voegeli.linusleo.synology.me"
                        unsubscribe_link = f"{base_url}/unsubscribe/{encoded_email}/"
                        email_body = (
                            "Hoi Du!<br>"
                            "I just came back and entered my birdhouse!<br>"
                            f"Check me out at {base_url}<br>"
                            "Best Regards, Your Vögeli<br><br>"
                            f'<a href="{unsubscribe_link}">Unsubscribe</a>'
                        )

                        try:
                            self.email_reporter.send_mail(
                                email_body,
                                subject="Vögeli Motion Alert",
                                recipients=email,
                                is_html=True,
                            )
                            sent_count += 1
                        except Exception:
                            logging.exception("Failed to send motion email to %s.", email)
            except FileNotFoundError:
                logging.info("No newsletter_subscribers.csv found, skipping motion email.")

            if sent_count > 0:
                with open(file_path, "w") as f:
                    f.write(str(int(current_time)))
                logging.info("Sent %d motion notification email(s).", sent_count)
            else:
                logging.info("No motion emails sent; last_email_sent not updated.")
        else:
            remaining = int(email_throttle_seconds - (current_time - last_email_time))
            logging.info("Motion email throttled for %ds.", max(0, remaining))

        # Save an image only if at least an hour has passed
        if current_time - self.last_image_time >= 3600:
            ir_enabled_for_capture = False
            latest_lux = None
            try:
                latest_lux = self.db_store.query_last(
                    data_since="5m",
                    field="luminosity",
                    unit="lux",
                )
            except Exception:
                logging.exception("Failed to read latest luminosity for IR gating.")

            if isinstance(latest_lux, (int, float)) and latest_lux > 2000:
                logging.info(
                    "Skipping IR for automated picture: luminosity %.2f lux > 2000 lux.",
                    float(latest_lux),
                )
            else:
                turn_ir_on()
                ir_enabled_for_capture = True
                time.sleep(3)

            timestamp = bern_image_timestamp()

            try:
                live_photo = self.rtsp_recorder.export_live_photo(
                    timestamp=timestamp,
                    output_dir="gallery",
                )
                if live_photo.warning:
                    logging.warning("Radar live image %s warning: %s", timestamp, live_photo.warning)
                self.last_image_time = current_time
                upload_live_photo(
                    live_photo_result=live_photo,
                    token=self.upload_image_token,
                    url=self.upload_image_url,
                )
                if live_photo.still_path is not None:
                    live_photo.still_path.unlink(missing_ok=True)
                if live_photo.motion_path is not None:
                    live_photo.motion_path.unlink(missing_ok=True)
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to capture image: {e.stderr}")
                print(f"Failed to capture image from MediaMTX server: {e}")
            except UploadImageError as e:
                logging.error("Failed to upload radar live image: %s", e)
            except subprocess.TimeoutExpired:
                logging.error("Timed out while capturing live image.")

            if ir_enabled_for_capture:
                turn_ir_off()

        print("Motion detected! Data stored.")

    def _presence_watcher(self) -> None:
        while not self._stop_event.is_set():
            self._presence_event.wait()
            if self._stop_event.is_set():
                break
            self._presence_event.clear()
            try:
                self.motion_detected_callback()
            except Exception:
                logging.exception("Unhandled exception in motion_detected_callback.")

    def store_radar_data(self, _state, presence_detected, presence_distance_m, breathing_rate_bpm, activity,
                         temperature):

        if presence_distance_m is not None:
            presence_distance_m = float(presence_distance_m)
        if activity is not None:
            activity = float(activity)
        if temperature is not None:
            temperature = float(temperature)
        if breathing_rate_bpm is not None:
            breathing_rate_bpm = float(breathing_rate_bpm)

        device_data = {
            'device': 'voegeli',
            'data': {
                # radar data
                'activity': activity,
                'activity_unit': 'score',
                'radar_inside_temperature': temperature,
                'radar_inside_temperature_unit': 'Celsius',
                'breathing_rate': breathing_rate_bpm,
                'breathing_rate_unit': 'bpm',
                'object_distance': presence_distance_m,
                'object_distance_unit': 'm',
                'motion': presence_detected,
            }
        }

        try:
            self.write_device_data_to_db(device_data)
        except (psycopg.Error, ConnectionError, OSError) as e:
            logging.warning(f"Database connection error, skipping this update: {e}")

    def _writer(self) -> None:
        next_write = time.time()
        while not self._stop_event.is_set():
            now = time.time()
            if now < next_write:
                time.sleep(min(0.05, next_write - now))
                continue
            with self._accum_lock:
                accum = self._accum
                self._accum = {
                    "count": 0,
                    "activity_sum": 0.0,
                    "temp_sum": 0.0,
                    "distance_sum": 0.0,
                    "distance_count": 0,
                    "bpm_sum": 0.0,
                    "bpm_count": 0,
                    "presence_any": False,
                }
            with self._latest_lock:
                sample = self._latest
            if accum["count"] > 0 and sample is not None:
                avg_activity = accum["activity_sum"] / accum["count"]
                avg_temp = accum["temp_sum"] / accum["count"]
                avg_distance = (
                    accum["distance_sum"] / accum["distance_count"]
                    if accum["distance_count"] > 0
                    else None
                )
                avg_bpm = (
                    accum["bpm_sum"] / accum["bpm_count"]
                    if accum["bpm_count"] > 0
                    else None
                )
                presence_any = accum["presence_any"]

                self.store_radar_data(sample.app_state, presence_any, avg_distance,
                                      avg_bpm, avg_activity, avg_temp)
            next_write += self.write_period_s


if __name__ == "__main__":
    Radar().run()
