import subprocess
import time
import psutil


class SystemMonitoring:

    def __init__(self):

        self.disk_size = "32G"
        self.disk_used = None
        self.disk_perc = None
        self.cpu_perc = None
        self.cpu_perc_cores = [None, None, None, None]
        self.uploaded_bytes_per_s = None
        self.downloaded_bytes_per_s = None
        self.memory_perc = None
        self.cpu_temp = None

    def monitor_system(self):

        last_uploaded_bytes = psutil.net_io_counters().bytes_sent
        last_downloaded_bytes = psutil.net_io_counters().bytes_recv
        last_measurement_time = time.time()
        psutil.cpu_percent()
        psutil.cpu_percent(percpu=True)

        time.sleep(10)

        while True:

            output = subprocess.check_output(["df", "-h"])

            # Convert the output to string and split it by lines
            lines = output.decode("utf-8").split("\n")

            # Loop through the lines to find the information for /dev/root
            for line in lines:
                if "/dev/root" in line:
                    # Split the line by whitespace and extract the required fields
                    fields = line.split()
                    self.disk_size = fields[1]
                    self.disk_used = fields[2]
                    self.disk_perc = float(fields[4].replace("%", ""))

            # Get CPU usage
            self.cpu_perc = psutil.cpu_percent()
            self.cpu_perc_cores = psutil.cpu_percent(percpu=True)

            # Get Network usage
            uploaded_bytes = psutil.net_io_counters().bytes_sent
            downloaded_bytes = psutil.net_io_counters().bytes_recv
            measurement_time = time.time()
            self.uploaded_bytes_per_s = (uploaded_bytes - last_uploaded_bytes) / (
                    measurement_time - last_measurement_time)
            self.downloaded_bytes_per_s = (downloaded_bytes - last_downloaded_bytes) / (
                    measurement_time - last_measurement_time)
            last_uploaded_bytes = uploaded_bytes
            last_downloaded_bytes = downloaded_bytes
            last_measurement_time = measurement_time

            # Get CPU temp
            self.cpu_temp = psutil.sensors_temperatures()['cpu_thermal'][0].current

            # Get memory usage
            memory = psutil.virtual_memory()
            self.memory_perc = memory.percent

            time.sleep(4)
