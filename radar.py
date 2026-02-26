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
import os

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
from urllib3.exceptions import HTTPError
import requests.exceptions
import influxdb_client.rest

import influxdb_client
from influxdb_client.client.write_api import SYNCHRONOUS

from camera import get_ir_led_state, turn_ir_on, turn_ir_off
from ignore_motion import are_we_still_blocked
from image_upload import upload_image


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
            frame_rate: float = 50.0,
            sweeps_per_frame: int = 8,
            hwaas: int = 64,
            start_m: float = 0.1,
            end_m: float = 0.35,
            lowest_bpm: float = 60.0,
            highest_bpm: float = 300.0,
            time_series_s: float = 5.0,
            num_distances: int = 1,
            distance_det_s: float = 8.0,
            write_period_s: float = 2.0,
            env_file: str = ".env",
    ) -> None:
        # Track last image save time and last email sent time
        self.last_image_time = 0

        env_values = dotenv_values(env_file)
        self.mediamtx_url = env_values['IMAGE_GRAB_URL']
        self.bucket = env_values['INFLUXDB_BUCKET']
        self.org = env_values['INFLUXDB_ORG']
        self.token = env_values['INFLUXDB_TOKEN']
        self.url = env_values['INFLUXDB_URL']
        self.upload_image_token = env_values['UPLOAD_IMAGE_TOKEN']
        self.upload_image_url = env_values['UPLOAD_IMAGE_URL']

        assert self.org and self.token and self.url and self.bucket, 'URL, Token, Org and Bucket must be defined in .env file'
        self.client = influxdb_client.InfluxDBClient(url=self.url, token=self.token, org=self.org, verify_ssl=False)

        # replace this with custom email-interface
        self.email_reporter = Reporter("Voegeli")

        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self.query_api = self.client.query_api()

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
        # Ignore near-field clutter and clamp to the configured max range
        self.min_presence_distance_m = start_m + 0.02
        self.max_presence_distance_m = end_m - 0.02
        self.min_activity_for_presence = 0.8

        self._client: Optional[a121.Client] = None
        self._processor: Optional[Processor] = None
        self._latest_lock = threading.Lock()
        self._latest: Optional[Sample] = None
        self._stop_event = threading.Event()
        self._presence_event = threading.Event()
        self._sampler_thread: Optional[threading.Thread] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._presence_thread: Optional[threading.Thread] = None
        self._presence_prev = False
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
        self._last_debug_log_s = 0.0

    def run(self) -> None:
        et.utils.config_logging()

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

        self._client = a121.Client.open(ip_address=self.ip_address, tcp_port=self.tcp_port)
        metadata = self._client.setup_session(
            a121.SessionConfig({self.sensor_id: sensor_config})
        )
        assert not isinstance(metadata, list)

        self._processor = Processor(
            sensor_config=sensor_config, processor_config=processor_config, metadata=metadata
        )

        self._client.start_session()

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
        if self._client is not None:
            self._client.stop_session()
            self._client.close()

    def _sampler(self) -> None:
        assert self._client is not None
        assert self._processor is not None
        while not self._stop_event.is_set():
            result = self._client.get_next()
            processor_result = self._processor.process(result)
            presence = processor_result.presence_result
            presence_distance = presence.presence_distance
            activity = max(presence.intra_presence_score, presence.inter_presence_score)
            presence_valid = (
                presence.presence_detected
                and self.min_presence_distance_m < presence_distance < self.max_presence_distance_m
                and activity >= self.min_activity_for_presence
            )

            if presence_valid and not self._presence_prev:
                self._presence_prev = True
                self._presence_event.set()
            elif not presence_valid:
                self._presence_prev = False

            temperature_c = result.temperature

            breathing_rate = None
            if processor_result.breathing_result is not None:
                breathing_rate = processor_result.breathing_result.breathing_rate

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
        assert self.bucket and self.org, 'Bucket and Org must be defined in .env file.'
        measurement_names = []

        if measurement is None:
            measurement = device_data['device']

        for key in device_data['data'].keys():
            if "unit" not in key and "location" not in key:
                measurement_names.append(str(key))
        points = []
        for mn in measurement_names:
            field = mn
            value = device_data['data'][mn]
            if value is not None:
                p = influxdb_client.Point(measurement).field(field, value)
                if f'{mn}_unit' in device_data['data'].keys():
                    unit = device_data['data'][f'{mn}_unit']
                    if unit is not None:
                        p.tag('unit', unit)
                if f'{mn}_location' in device_data['data'].keys():
                    location = device_data['data'][f'{mn}_location']
                    if location is not None:
                        p.tag('location', location)
                if f'{mn}_type' in device_data['data'].keys():
                    type_tag = device_data['data'][f'{mn}_type']
                    if type_tag is not None:
                        p.tag('type', type_tag)
                points.append(p)
        self.write_api.write(bucket=self.bucket, org=self.org, record=points)

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

        # Save an image only if at least an hour has passed
        if current_time - self.last_image_time >= 3600:
            turn_ir_on()
            time.sleep(3)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            image_path = os.path.join("gallery", f"{timestamp}.jpg")

            try:

                subprocess.run([
                    "ffmpeg",
                    "-rtsp_transport", "tcp",
                    "-i", self.mediamtx_url,
                    "-frames:v", "1",
                    "-q:v", "2",
                    "-y",
                    image_path
                ], check=True, capture_output=True, text=True)

                self.last_image_time = current_time
                upload_image(image_path=image_path, token=self.upload_image_token,
                             url=self.upload_image_url)
                os.remove(image_path)
            except subprocess.CalledProcessError as e:
                logging.error(f"Failed to capture image: {e.stderr}")
                print(f"Failed to capture image from MediaMTX server: {e}")

            turn_ir_off()

        # Send an email only if at least a day has passed
        file_path = "last_email_sent.txt"

        # Read the existing timestamp or initialize it
        try:
            with open(file_path, "r+") as f:
                content = f.readline().strip()
                last_email_time = int(content) if content else 0  # Convert to int, default to 0 if empty
                if current_time - last_email_time >= 86400:
                    try:
                        csv_file = 'newsletter_subscribers.csv'
                        with open(csv_file, mode='r') as file:
                            reader = csv.reader(file)
                            subscribers = list(reader)
                            for subscriber in subscribers:
                                email = subscriber[0]
                                encoded_email = encode_email(email)

                                base_url = "https://linusleo.synology.me"
                                unsubscribe_link = f"{base_url}/unsubscribe/{encoded_email}/"
                                email_body = (
                                    "Hoi Du!<br>"
                                    "I just came back and entered my birdhouse!<br>"
                                    f"Check me out at {base_url}<br>"
                                    "Best Regards, Your Vögeli<br><br>"
                                    f'<a href="{unsubscribe_link}">Unsubscribe</a>'
                                )

                                self.email_reporter.send_mail(
                                    email_body,
                                    subject="Vögeli Motion Alert",
                                    recipients=email,
                                    is_html=True,
                                )
                    except FileNotFoundError:
                        pass  # File does not exist yet, no subscribers
                    # Overwrite with the new timestamp
                    f.seek(0)  # Move to the beginning of the file
                    new_timestamp = int(current_time)
                    f.write(str(new_timestamp))
                    f.truncate()  # Remove any leftover content after the new write
        except FileNotFoundError:
            # If the file doesn't exist, create it and write the timestamp
            with open(file_path, "w") as f:
                new_timestamp = int(current_time)
                last_email_time = 0
                f.write(str(new_timestamp))

        print("Motion detected! Data stored.")

    def _presence_watcher(self) -> None:
        while not self._stop_event.is_set():
            self._presence_event.wait()
            if self._stop_event.is_set():
                break
            self._presence_event.clear()
            self.motion_detected_callback()

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
        except (HTTPError,
                requests.exceptions.ConnectionError,
                requests.exceptions.ConnectTimeout,
                influxdb_client.rest.ApiException,
                ConnectionError,
                OSError) as e:
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
                if now - self._last_debug_log_s >= 10.0:
                    self._last_debug_log_s = now
                    distance_str = f"{avg_distance:.3f}m" if avg_distance is not None else "None"
                    bpm_str = f"{avg_bpm:.1f}" if avg_bpm is not None else "None"
                    logging.info(
                        "[radar] presence=%s distance=%s activity=%.3f bpm=%s state=%s count=%d",
                        presence_any,
                        distance_str,
                        avg_activity,
                        bpm_str,
                        sample.app_state,
                        accum["count"],
                    )
            elif now - self._last_debug_log_s >= 10.0:
                self._last_debug_log_s = now
                logging.info("[radar] no samples yet")
            next_write += self.write_period_s


if __name__ == "__main__":
    Radar().run()
