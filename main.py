import queue
import subprocess
from pathlib import Path
import logging

import busio

import time
import board
import adafruit_sht4x, adafruit_scd4x, adafruit_tsl2561
from sensirion_i2c_driver import LinuxI2cTransceiver, I2cConnection, CrcCalculator
from sensirion_driver_adapters.i2c_adapter.i2c_channel import I2cChannel
from sensirion_i2c_sht4x.device import Sht4xDevice

import csv
import datetime
import os
import threading

from image_upload import upload_image
from radar import Radar
from time_utils import bern_image_timestamp
from system_monitor import SystemMonitoring
from camera import turn_ir_on, turn_ir_off, get_ir_led_state

from dotenv import dotenv_values
import psycopg

import urllib3

from tcp_server import run_server
from postgresql_store import PostgresTimeSeriesStore

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import numpy as np
import joblib


class VoegeliMonitor:
    _TSL2561_CLIP_THRESHOLD = (4900, 37000, 65000)

    def __init__(self, env_file: Path = Path('./.env')):

        self.task_is_running = True

        self.model_rise = joblib.load("models/bird_model_rise.pkl")
        self.model_fall = joblib.load("models/bird_model_fall.pkl")

        env_values = dotenv_values(env_file)
        self.mediamtx_url = env_values['IMAGE_GRAB_URL']
        self.db_store = PostgresTimeSeriesStore(env_values)
        self.bucket = self.db_store.bucket
        self.upload_image_token = env_values['UPLOAD_IMAGE_TOKEN']
        self.upload_image_url = env_values['UPLOAD_IMAGE_URL']

        # I2C sensor setup
        i2c = board.I2C()

        # SHT4x Temperature and Humidity Sensor inside
        self.sht_inside = adafruit_sht4x.SHT4x(i2c)

        # SHT4x Temperature and Humidity Sensor inside
        self.sht4x_outside_transceiver = LinuxI2cTransceiver("/dev/i2c-3")
        self.sht4x_outside_channel = I2cChannel(
            I2cConnection(self.sht4x_outside_transceiver),
            slave_address=0x44,  # or 0x45 if needed
            crc=CrcCalculator(8, 0x31, 0xFF, 0x00),
        )
        self.sht_outside = Sht4xDevice(self.sht4x_outside_channel)

        # CO2 sensor inside
        self.co2_sensor = adafruit_scd4x.SCD4X(i2c)
        self.co2_sensor.start_periodic_measurement()

        # Luminosity sensor
        self.luminosity_sensor = adafruit_tsl2561.TSL2561(i2c)

        # motion sensor (A121 radar 60 GHz)
        self.radar = Radar()
        self.radar.run()

        # self.audio_stream_thread = threading.Thread(target=run_audiostream)
        # self.audio_stream_thread.daemon = True
        # self.audio_stream_thread.start()

        self.system_monitoring = SystemMonitoring()
        self.sys_monitoring_thread = threading.Thread(target=self.system_monitoring.monitor_system)
        self.sys_monitoring_thread.daemon = True
        self.sys_monitoring_thread.start()

        # Start data logger thread
        data_thread = threading.Thread(target=self.periodic_data_logger, daemon=True)
        data_thread.start()

        self.tcp_cmd_queue = queue.Queue()
        self.tcp_cmd_ack_queue = queue.Queue()
        self.tcp_rep_queue = queue.Queue()

        self.tcp_server = run_server(self.tcp_cmd_queue, self.tcp_cmd_ack_queue, self.tcp_rep_queue,
                                     env_file,
                                     self.task_is_running,
                                     False,
                                     port=65432,
                                     ip="0.0.0.0")

    def shutdown(self):
        try:
            self.sht4x_outside_transceiver.close()
        except Exception:
            pass
        self.db_store.close()

    def send_tcp_ack(self, message: str, response_queue: queue.Queue | None = None):
        if response_queue is not None:
            response_queue.put(message)
        else:
            # Backward-compatible fallback path.
            self.tcp_cmd_ack_queue.put(message)
        logging.info("[TCP] Sent ACK: %s", message.strip())

    def send_tcp_rep(self, message: str):
        self.tcp_rep_queue.put(message)
        logging.info("[TCP] Sent REP: %s", message.strip())

    # Function to read temperature and humidity
    def read_temperature_humidity(self, sensor, sensirion=False):
        if sensirion:
            t, rh = sensor.measure_lowest_precision()
            return round(t.value, 2), round(rh.value, 2)
        else:
            return round(sensor.temperature, 2), round(sensor.relative_humidity, 2)

    def read_co2_sensor(self, scd4x):
        if scd4x.data_ready:
            return float(scd4x.CO2), round(scd4x.temperature, 2), round(scd4x.relative_humidity, 2)
        else:
            return None, None, None

    def _read_tsl2561_raw_channels(self, tsl2561):
        broadband = getattr(tsl2561, "broadband", None)
        infrared = getattr(tsl2561, "infrared", None)
        if broadband is not None and infrared is not None:
            return int(broadband), int(infrared)

        luminosity = getattr(tsl2561, "luminosity", None)
        if isinstance(luminosity, (tuple, list)) and len(luminosity) >= 2:
            return int(luminosity[0]), int(luminosity[1])

        return None, None

    def read_luminosity_sensor(self, tsl2561):
        try:
            luminosity = tsl2561.lux
        except Exception:
            logging.warning("TSL2561 read failed.", exc_info=True)
            return None, None, None

        broadband, infrared = self._read_tsl2561_raw_channels(tsl2561)

        if luminosity is not None:
            return round(luminosity, 2), broadband, infrared

        if broadband is None or infrared is None:
            return None, None, None

        # Mirrors driver behavior: ch0==0 means too dark to compute lux.
        if broadband == 0:
            return 0.0, 0, 0

        try:
            integration_time = int(getattr(tsl2561, "integration_time", 2))
        except Exception:
            integration_time = 2
        if integration_time < 0 or integration_time >= len(self._TSL2561_CLIP_THRESHOLD):
            integration_time = 2
        clip_threshold = self._TSL2561_CLIP_THRESHOLD[integration_time]

        # Mirrors driver behavior: clipped channels are saturated.
        if broadband > clip_threshold or infrared > clip_threshold:
            logging.debug(
                "TSL2561 saturated (broadband=%s infrared=%s threshold=%s integration_time=%s).",
                broadband,
                infrared,
                clip_threshold,
                integration_time,
            )
            return None, None, None

        # Defensive fallback for edge cases where lux is still not computable.
        return 0.0, 0, 0

    def query_database_last(self, data_since='1m', bucket=None, field='heating_set_temperature', unit="Kelvin"):
        """Query a database field during the specified time period."""
        return self.db_store.query_last(
            data_since=data_since,
            bucket=bucket,
            field=field,
            unit=unit,
        )

    def write_device_data_to_db(self, device_data, measurement=None):
        self.db_store.write_device_data(device_data, measurement=measurement)

    # Function to store sensor data in the database
    def store_sensor_data(self, inside_temperature, inside_humidity, outside_temperature, outside_humidity, inside_co2,
                          inside_co2_temperature, inside_co2_humidity,
                          luminosity, broadband, infrared,
                          motion_triggered):

        if luminosity is None:
            probability = 0.99
        else:
            if datetime.datetime.now().hour > 12:
                probability = self.model_rise.predict_proba([[luminosity]])[0, 1]
            else:
                probability = self.model_fall.predict_proba([[luminosity]])[0, 1]

        probability = np.clip(probability, 0.01, 0.99)

        device_data = {
            'device': 'voegeli',
            'data': {
                # system monitoring of Raspberry Pi

                'disk_size': self.system_monitoring.disk_size,
                'disk_used': self.system_monitoring.disk_used,
                'disk_perc': self.system_monitoring.disk_perc,
                'cpu_perc': self.system_monitoring.cpu_perc,
                'core_1_perc': self.system_monitoring.cpu_perc_cores[0],
                'core_2_perc': self.system_monitoring.cpu_perc_cores[1],
                'core_3_perc': self.system_monitoring.cpu_perc_cores[2],
                'core_4_perc': self.system_monitoring.cpu_perc_cores[3],
                'cpu_temp': self.system_monitoring.cpu_temp,
                'uploaded_bytes_per_s': self.system_monitoring.uploaded_bytes_per_s,
                'downloaded_bytes_per_s': self.system_monitoring.downloaded_bytes_per_s,
                'memory_perc': self.system_monitoring.memory_perc,
                # ambient data

                'outside_temperature': outside_temperature,
                'outside_temperature_unit': 'Celsius',
                'outside_humidity': outside_humidity,
                'outside_humidity_unit': '%',
                'inside_temperature': inside_temperature,
                'inside_temperature_unit': 'Celsius',
                'inside_humidity': inside_humidity,
                'inside_humidity_unit': '%',
                'inside_co2': inside_co2,
                'inside_co2_unit': 'ppm',
                'inside_co2_temperature': inside_co2_temperature,
                'inside_co2_temperature_unit': 'Celsius',
                'inside_co2_humidity_f': inside_co2_humidity,
                'inside_co2_humidity_f_unit': '%',
                'luminosity': luminosity,
                'luminosity_unit': 'lux',
                'broadband_luminosity': broadband,
                'IR_luminosity': infrared,
                'probability': probability,
            }
        }

        try:
            self.write_device_data_to_db(device_data)
        except (psycopg.Error, ConnectionError, OSError) as e:
            logging.warning(f"Database connection error, skipping this update: {e}")

    # Background thread for temperature/humidity logging (runs every 60s)
    def periodic_data_logger(self):
        logging.info("Periodic data logger started.")
        turn_off_ir_led = None
        prev_ir_led_state = get_ir_led_state()
        while True:
            try:
                inside_temperature, inside_humidity = self.read_temperature_humidity(self.sht_inside)
                outside_temperature, outside_humidity = self.read_temperature_humidity(
                    self.sht_outside,
                    sensirion=True
                )

                logging.debug("Reading CO2 sensor.")
                inside_co2, inside_co2_temperature, inside_co2_humidity = self.read_co2_sensor(self.co2_sensor)

                logging.debug("Reading luminosity sensor.")
                lux, broadband, infrared = self.read_luminosity_sensor(self.luminosity_sensor)

                logging.debug("Storing sensor data to PostgreSQL.")
                self.store_sensor_data(inside_temperature, inside_humidity,
                                       outside_temperature, outside_humidity,
                                       inside_co2, inside_co2_temperature, inside_co2_humidity,
                                       lux,
                                       broadband,
                                       infrared,
                                       motion_triggered=False)

                ir_led_state = get_ir_led_state()

                # Start a fresh auto-off timer on each OFF->ON transition.
                if ir_led_state and not prev_ir_led_state:
                    turn_off_ir_led = time.time() + 5 * 60

                # Clear stale deadline when LED is already OFF.
                if not ir_led_state:
                    turn_off_ir_led = None

                if turn_off_ir_led is not None and turn_off_ir_led < time.time() and ir_led_state:
                    turn_off_ir_led = None
                    self.send_tcp_rep("[REP] IR LED STATE: OFF")
                    turn_ir_off()

                prev_ir_led_state = ir_led_state
            except Exception:
                logging.exception("Periodic data logger error.")
            time.sleep(10)


if __name__ == "__main__":

    logging.basicConfig(filename=f"log/log_{time.time()}.log",
                        filemode='a',
                        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S',
                        level=logging.DEBUG)
    logging.getLogger("sensirion_i2c_driver").setLevel(logging.INFO)
    logging.getLogger("sensirion_i2c_driver.connection").setLevel(logging.INFO)

    voegeli_monitor = VoegeliMonitor()

    old_ir_led_state = False
    old_ir_filter_state = False

    while True:
        try:
            cmd_packet = voegeli_monitor.tcp_cmd_queue.get(timeout=0.1)
            response_queue = None
            if isinstance(cmd_packet, tuple) and len(cmd_packet) == 2 and hasattr(cmd_packet[1], "put"):
                cmd = cmd_packet[0]
                response_queue = cmd_packet[1]
            else:
                cmd = cmd_packet
            logging.debug(f"[TCP] revived: {cmd}")
            cmd_string = cmd.decode("utf-8", errors="replace") if isinstance(cmd, bytes) else str(cmd)

            def send_ack(message: str):
                voegeli_monitor.send_tcp_ack(message, response_queue=response_queue)

            if "[CMD] IR ON" in cmd_string:
                turn_ir_on()
                send_ack("[ACK] IR ON executed")
            elif "[CMD] IR OFF" in cmd_string:
                turn_ir_off()
                send_ack("[ACK] IR OFF executed")
            elif "[CMD] GET IR STATE" in cmd_string:
                ir_state = get_ir_led_state()
                send_ack(f"[ACK] IR STATE is {'ON' if ir_state else 'OFF'}")
            elif "[CMD] add newsletter=" in cmd_string:
                email = cmd_string.split('=', 1)[1].strip()
                csv_file = 'newsletter_subscribers.csv'
                # Check if the email is already in the file
                email_exists = False
                try:
                    with open(csv_file, mode='r') as file:
                        reader = csv.reader(file)
                        for row in reader:
                            if row and row[0] == email:
                                email_exists = True
                                break
                except FileNotFoundError:
                    pass  # File does not exist yet

                if not email_exists:
                    with open(csv_file, mode='a', newline='') as file:
                        writer = csv.writer(file)
                        writer.writerow([email])
                    send_ack(f"[ACK] Email {email} added to newsletter")
                else:
                    send_ack(f"[ACK] Email {email} already in newsletter")
            elif "[CMD] remove newsletter=" in cmd_string:
                email = cmd_string.split('=', 1)[1].strip()
                csv_file = 'newsletter_subscribers.csv'
                # Read all emails and filter out the one to remove
                emails = []
                try:
                    with open(csv_file, mode='r') as file:
                        reader = csv.reader(file)
                        for row in reader:
                            if row and row[0] != email:
                                emails.append(row[0])
                    # Write back the filtered list
                    with open(csv_file, mode='w', newline='') as file:
                        writer = csv.writer(file)
                        for em in emails:
                            writer.writerow([em])
                    send_ack(f"[ACK] Email {email} removed from newsletter")
                except FileNotFoundError:
                    send_ack(f"[ACK] Newsletter file not found")
            elif "[CMD] save image" in cmd_string:
                timestamp = bern_image_timestamp()
                image_path = os.path.join("gallery", f"{timestamp}.jpg")
                try:

                    subprocess.run([
                        "ffmpeg",
                        "-rtsp_transport", "tcp",
                        "-i", voegeli_monitor.mediamtx_url,
                        "-frames:v", "1",
                        "-q:v", "2",
                        "-y",
                        image_path
                    ], check=True, capture_output=True, text=True)
                    send_ack(f"[ACK] Image saved to {image_path}")
                    upload_image(image_path=image_path, token=voegeli_monitor.upload_image_token,
                                 url=voegeli_monitor.upload_image_url)
                    os.remove(image_path)
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to save image: {e.stderr}")
                    send_ack(f"[ACK] Failed to save image: MediaMTX server error")
        except queue.Empty:
            pass

        if old_ir_led_state != get_ir_led_state():
            logging.info(f"IR LED state changed to {'ON' if get_ir_led_state() else 'OFF'}")
            voegeli_monitor.send_tcp_rep("[REP] IR LED STATE: " + ('ON' if get_ir_led_state() else 'OFF'))
            old_ir_led_state = get_ir_led_state()

        # if old_ir_filter_state != get_ir_filter_state():
        #     logging.info(f"IR Filter state changed to {'ON' if get_ir_filter_state() else 'OFF'}")
        # .   voegeli_monitor.tcp_rep_queue.put("[REP] IR FILTER STATE: " + ('ON' if get_ir_filter_state() else 'OFF'))
        #     old_ir_filter_state = get_ir_filter_state()
