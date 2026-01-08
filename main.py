import queue
import subprocess
from pathlib import Path
import logging

import busio
from gpiozero import MotionSensor

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

from audio_stream import run_audiostream
from system_monitor import SystemMonitoring
from ignore_motion import are_we_still_blocked
from camera import turn_ir_on, turn_ir_off, get_ir_led_state, get_ir_filter_state, turn_ir_filter_off, turn_ir_filter_on
from encoding import encode_email

from unibe_mail import Reporter
from dotenv import dotenv_values

import urllib3

from tcp_server import run_server

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from urllib3.exceptions import HTTPError
import requests.exceptions
import influxdb_client.rest

import influxdb_client
from influxdb_client.client.write_api import SYNCHRONOUS


class VoegeliMonitor:
    def __init__(self, env_file: Path = Path('./.env')):

        self.task_is_running = True
        # Track last image save time and last email sent time
        self.last_image_time = 0

        env_values = dotenv_values(env_file)
        self.mediamtx_url = env_values['IMAGE_GRAB_URL']
        self.bucket = env_values['INFLUXDB_BUCKET']
        self.org = env_values['INFLUXDB_ORG']
        self.token = env_values['INFLUXDB_TOKEN']
        self.url = env_values['INFLUXDB_URL']

        assert self.org and self.token and self.url and self.bucket, 'URL, Token, Org and Bucket must be defined in .env file'
        self.client = influxdb_client.InfluxDBClient(url=self.url, token=self.token, org=self.org, verify_ssl=False)

        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self.query_api = self.client.query_api()

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

        # GPIO Motion Sensor Setup
        MOTION_PIN = 4
        self.pir = MotionSensor(MOTION_PIN, threshold=0.8, queue_len=10)

        # email callback

        # replace this with custom email-interface
        self.email_reporter = Reporter("Voegeli")

        self.audio_stream_thread = threading.Thread(target=run_audiostream)
        self.audio_stream_thread.daemon = True
        self.audio_stream_thread.start()

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

    # Function to read temperature and humidity
    def read_temperature_humidity(self, sensor, sensirion=False):
        if sensirion:
            t, rh = sensor.measure_lowest_precision()
            return round(t.value, 2), round(rh.value, 2)
        else:
            return round(sensor.temperature, 2), round(sensor.relative_humidity, 2)

    def read_co2_sensor(self, scd4x):
        if scd4x.data_ready:
            return round(scd4x.temperature, 2), round(scd4x.relative_humidity, 2), scd4x.CO2
        else:
            return None, None, None

    def read_luminosity_sensor(self, tsl2561):
        luminosity = tsl2561.lux
        if luminosity is not None:
            return round(luminosity, 2)
        else:
            return None

    def query_database_last(self, data_since='1m', bucket='COCoNuT', field='heating_set_temperature', unit="Kelvin"):
        """Query a database field during the specified time period."""

        query = f'from(bucket: "{bucket}")\
        |> range(start: -{data_since})\
        |> filter(fn: (r) => r["_field"] == "{field}")\
        |> filter(fn: (r) => r["unit"] == "{unit}")\
        |> last()'

        result = self.query_api.query(query=query)

        temperatures = []
        for table in result:
            for record in table.records:
                temperatures.append(record.values['_value'])

        return temperatures[-1]

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
    def store_sensor_data(self, inside_temperature, inside_humidity, outside_temperature, outside_humidity, inside_co2,
                          inside_co2_temperature, inside_co2_humidity,
                          luminosity,
                          motion_triggered):
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
                'inside_co2_humidity': inside_co2_humidity,
                'inside_co2_humidity_unit': '%',
                'luminosity': luminosity,
                'luminosity_unit': 'lux',
                'motion': motion_triggered,
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

    def motion_detected_callback(self):

        # Check if motion detection should be ignored
        if are_we_still_blocked():
            print("Motion detection temporarily ignored.")
            return
        if get_ir_led_state():
            print("Motion detection ignored because IR LED is on.")
            return

        current_time = time.time()
        inside_temperature, inside_humidity = self.read_temperature_humidity(self.sht_inside)
        outside_temperature, outside_humidity = self.read_temperature_humidity(
            self.sht_outside,
            sensirion=True
        )
        inside_co2, inside_co2_temperature, inside_co2_humidity = self.read_co2_sensor(self.co2_sensor)
        luminosity = self.read_luminosity_sensor(self.luminosity_sensor)
        self.store_sensor_data(inside_temperature, inside_humidity,
                               outside_temperature, outside_humidity,
                               inside_co2, inside_co2_temperature, inside_co2_humidity,
                               luminosity,
                               motion_triggered=True)

        # Save an image only if at least an hour has passed
        if current_time - self.last_image_time >= 3600:
            turn_ir_on()
            time.sleep(3)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            image_path = os.path.join("gallery", f"{timestamp}.jpg")

            try:
                if not get_ir_filter_state():
                    subprocess.run([
                        "ffmpeg",
                        "-i", self.mediamtx_url,
                        "-vf", "format=gray",
                        "-frames:v", "1",
                        image_path
                    ], check=True, capture_output=True, text=True)
                else:
                    subprocess.run([
                        "ffmpeg",
                        "-i", self.mediamtx_url,
                        "-frames:v", "1",
                        image_path
                    ], check=True, capture_output=True, text=True)

                self.last_image_time = current_time
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

    time.sleep(1)  # Wait for hardware to settle

    # Background thread for temperature/humidity logging (runs every 60s)
    def periodic_data_logger(self):
        # Register interrupt for motion detection (FALLING or RISING can be used)
        self.pir.when_motion = self.motion_detected_callback
        turn_off_ir_led = None
        while True:
            inside_temperature, inside_humidity = self.read_temperature_humidity(self.sht_inside)
            outside_temperature, outside_humidity = self.read_temperature_humidity(
                self.sht_outside,
                sensirion=True
            )
            inside_co2, inside_co2_temperature, inside_co2_humidity = self.read_co2_sensor(self.co2_sensor)
            luminosity = self.read_luminosity_sensor(self.luminosity_sensor)
            self.store_sensor_data(inside_temperature, inside_humidity,
                                   outside_temperature, outside_humidity,
                                   inside_co2, inside_co2_temperature, inside_co2_humidity,
                                   luminosity,
                                   motion_triggered=True)

            if turn_off_ir_led is None and get_ir_led_state():
                # set turn-off to now + 5 minutes
                turn_off_ir_led = time.time() + 5 * 60
            if turn_off_ir_led is not None and turn_off_ir_led < time.time() and get_ir_led_state():
                turn_off_ir_led = None
                voegeli_monitor.tcp_rep_queue.put("[REP] IR LED STATE: OFF")
                turn_ir_off()
            time.sleep(10)


if __name__ == "__main__":

    logging.basicConfig(filename=f"log/log_{time.time()}.log",
                        filemode='a',
                        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S',
                        level=logging.DEBUG)

    voegeli_monitor = VoegeliMonitor()

    old_ir_led_state = False
    old_ir_filter_state = False

    while True:
        try:
            cmd = voegeli_monitor.tcp_cmd_queue.get(block=False)
            logging.debug(f"[TCP] revived: {cmd}")
            cmd_string = cmd
            if "[CMD] IR ON" in cmd_string:
                turn_ir_on()
                voegeli_monitor.tcp_cmd_ack_queue.put("[ACK] IR ON executed")
            elif "[CMD] IR OFF" in cmd_string:
                turn_ir_off()
                voegeli_monitor.tcp_cmd_ack_queue.put("[ACK] IR OFF executed")
            elif "[CMD] GET IR STATE" in cmd_string:
                ir_state = get_ir_led_state()
                voegeli_monitor.tcp_cmd_ack_queue.put(f"[ACK] IR STATE is {'ON' if ir_state else 'OFF'}")
            elif "[CMD] IR FILTER ON" in cmd_string:
                turn_ir_filter_on()
                voegeli_monitor.tcp_cmd_ack_queue.put("[ACK] IR FILTER ON executed")
            elif "[CMD] IR FILTER OFF" in cmd_string:
                turn_ir_filter_off()
                voegeli_monitor.tcp_cmd_ack_queue.put("[ACK] IR FILTER OFF executed")
            elif "[CMD] GET IR FILTER STATE" in cmd_string:
                ir_filter_state = get_ir_filter_state()
                voegeli_monitor.tcp_cmd_ack_queue.put(f"[ACK] IR FILTER STATE is {'ON' if ir_filter_state else 'OFF'}")
            elif "[CMD] add newsletter=" in cmd_string:
                email = cmd_string.split(b'=')[1].strip()
                csv_file = 'newsletter_subscribers.csv'
                # Check if the email is already in the file
                email_exists = False
                try:
                    with open(csv_file, mode='r') as file:
                        reader = csv.reader(file)
                        for row in reader:
                            if row and row[0].encode() == email:
                                email_exists = True
                                break
                except FileNotFoundError:
                    pass  # File does not exist yet

                if not email_exists:
                    with open(csv_file, mode='a', newline='') as file:
                        writer = csv.writer(file)
                        writer.writerow([email.decode()])
                    voegeli_monitor.tcp_cmd_ack_queue.put(f"[ACK] Email {email.decode()} added to newsletter")
                else:
                    voegeli_monitor.tcp_cmd_ack_queue.put(f"[ACK] Email {email.decode()} already in newsletter")
            elif "[CMD] remove newsletter=" in cmd_string:
                email = cmd_string.split(b'=')[1].strip()
                csv_file = 'newsletter_subscribers.csv'
                # Read all emails and filter out the one to remove
                emails = []
                try:
                    with open(csv_file, mode='r') as file:
                        reader = csv.reader(file)
                        for row in reader:
                            if row and row[0].encode() != email:
                                emails.append(row[0])
                    # Write back the filtered list
                    with open(csv_file, mode='w', newline='') as file:
                        writer = csv.writer(file)
                        for em in emails:
                            writer.writerow([em])
                    voegeli_monitor.tcp_cmd_ack_queue.put(f"[ACK] Email {email.decode()} removed from newsletter")
                except FileNotFoundError:
                    voegeli_monitor.tcp_cmd_ack_queue.put(f"[ACK] Newsletter file not found")
                    elif "[CMD] save image" in cmd_string:
                    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    image_path = os.path.join("gallery", f"{timestamp}.jpg")

                    try:
                        if not get_ir_filter_state():
                            subprocess.run([
                                "ffmpeg",
                                "-i", voegeli_monitor.mediamtx_url,
                                "-vf", "format=gray",
                                "-frames:v", "1",
                                image_path
                            ], check=True, capture_output=True, text=True)
                        else:
                            subprocess.run([
                                "ffmpeg",
                                "-i", voegeli_monitor.mediamtx_url,
                                "-frames:v", "1",
                                image_path
                            ], check=True, capture_output=True, text=True)
                        voegeli_monitor.tcp_cmd_ack_queue.put(f"[ACK] Image saved to {image_path}")
                    except subprocess.CalledProcessError as e:
                        logging.error(f"Failed to save image: {e.stderr}")
                        voegeli_monitor.tcp_cmd_ack_queue.put(f"[ACK] Failed to save image: MediaMTX server error")
        except queue.Empty:
            time.sleep(1)

        if old_ir_led_state != get_ir_led_state():
            logging.info(f"IR LED state changed to {'ON' if get_ir_led_state() else 'OFF'}")
            voegeli_monitor.tcp_rep_queue.put("[REP] IR LED STATE: " + ('ON' if get_ir_led_state() else 'OFF'))
            old_ir_led_state = get_ir_led_state()

        # if old_ir_filter_state != get_ir_filter_state():
        #     logging.info(f"IR Filter state changed to {'ON' if get_ir_filter_state() else 'OFF'}")
        # .   voegeli_monitor.tcp_rep_queue.put("[REP] IR FILTER STATE: " + ('ON' if get_ir_filter_state() else 'OFF'))
        #     old_ir_filter_state = get_ir_filter_state()
