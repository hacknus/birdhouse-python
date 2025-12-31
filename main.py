import subprocess
from pathlib import Path
import logging

from gpiozero import MotionSensor

import time
import board
import adafruit_sht4x

import csv
import datetime
import os
import threading

from audio_stream import run_audiostream
from system_monitor import SystemMonitoring
from ignore_motion import are_we_still_blocked
from camera import turn_ir_on, turn_ir_off, get_ir_led_state
from encoding import encode_email

from unibe_mail import Reporter
from dotenv import dotenv_values

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from urllib3.exceptions import HTTPError
import requests.exceptions
import influxdb_client.rest

import influxdb_client
from influxdb_client.client.write_api import SYNCHRONOUS

class VoegeliMonitor:
    def __init__(self, env_file: Path = Path('./.env')):

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
        # self.sht_outside = adafruit_sht4x.SHT4x(i2c)

        # CO2 sensor inside
        # ...

        # GPIO Motion Sensor Setup
        MOTION_PIN = 4
        self.pir = MotionSensor(MOTION_PIN, threshold=0.8, queue_len=10)

        # email callback

        # replace this with custom email-interface
        self.email_reporter = Reporter("Voegeli")

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

    # Function to read temperature and humidity
    def read_temperature_humidity(self, sensor):
        temperature = round(sensor.temperature, 2)
        humidity = round(sensor.relative_humidity, 2)
        return temperature, humidity

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

    def write_value_to_db(self, value, name, unit, location="Gallery B32", measurement=None):
        assert self.bucket and self.org, 'Bucket and Org must be defined in .env file.'
        if measurement is None:
            measurement = self.measurement_name
        field = name
        p = influxdb_client.Point(measurement).tag("location", location).tag("unit", unit).field(field, value)
        self.write_api.write(bucket=self.bucket, org=self.org, record=p)

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
                          motion_triggered):
        device_data = {
            'device': 'COCoNuT-ELU',
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

                # 'outside_temperature': outside_temperature,
                # 'outside_temperature_unit': 'Celsius',
                # 'outside_humidity': outside_humidity,
                # 'outside_humidity_unit': '%',
                'inside_temperature': inside_temperature,
                'inside_temperature_unit': 'Celsius',
                'inside_humidity': inside_humidity,
                'inside_humidity_unit': '%',
                # 'inside_co2': inside_co2,
                # 'inside_co2_unit': 'ppm',
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

    # Track last image save time and last email sent time
    last_image_time = 0

    def motion_detected_callback(self):
        global last_image_time

        # Check if motion detection should be ignored
        if are_we_still_blocked():
            print("Motion detection temporarily ignored.")
            return
        if get_ir_led_state():
            print("Motion detection ignored because IR LED is on.")
            return

        current_time = time.time()
        inside_temperature, inside_humidity = self.read_temperature_humidity(self.sht_inside)
        outside_temperature, outside_humidity = 0, 0  # self.read_temperature_humidity(self.sht_outside)
        inside_co2 = 0  # read_co2_sensor()
        self.store_sensor_data(inside_temperature, inside_humidity,
                               outside_temperature, outside_humidity,
                               inside_co2,
                               motion_triggered=True)

        # Save an image only if at least an hour has passed
        if current_time - last_image_time >= 3600:
            # todo: only do this if the light is not on
            turn_ir_on()
            time.sleep(3)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            image_path = os.path.join("gallery", f"{timestamp}.jpg")

            subprocess.run([
                "ffmpeg",
                "-i", self.mediamtx_url,
                "-frames:v", "1",
                image_path
            ], check=True)

            # frame = camera_stream.get_frame()
            #
            # cv2.imwrite(image_path, frame)

            last_image_time = current_time

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
            outside_temperature, outside_humidity = 0, 0  # self.read_temperature_humidity(self.sht_outside)
            inside_co2 = 0  # read_co2_sensor()
            self.store_sensor_data(inside_temperature, inside_humidity,
                                   outside_temperature, outside_humidity,
                                   inside_co2,
                                   motion_triggered=False)
            if turn_off_ir_led is None and get_ir_led_state():
                # set turn-off to now + 5 minutes
                turn_off_ir_led = time.time() + 5 * 60
            if turn_off_ir_led is not None and turn_off_ir_led < time.time() and get_ir_led_state():
                turn_off_ir_led = None
                turn_ir_off()
            time.sleep(60)


if __name__ == "__main__":
    voegeli_monitor = VoegeliMonitor()
    while True:
        time.sleep(1)