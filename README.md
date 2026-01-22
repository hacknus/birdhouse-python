# Birdhouse

The birdhouse project consists sof two parts:

- birdhouse-rs, the webserver
- birdhouse-python, the camera control hosted on the raspberry pi in the birdhouse.

![birdhouse website](docs/screenshot.png)

The webserver is built with [Dioxus](https://dioxuslabs.com/).
The video feed is streamed to mediamtx and then proxied through the webserver with webRTC and using a turn server (
coturn).

Multiple docker container are required for this project:

- dioxus webserver
- mediamtx server
- coturn server
- grafana

The raspberry pi and the server are connected using netbird.

![structure](docs/birdhouse.drawio.svg)

## Setting it up:

First, set up [birdhouse-rs](https://github.com/hacknus/birdhouse-rs) on a server (or synology NAS).
Then set up netbird on both the raspberry pi and the server.

Enter all tokens and URLs in the `.env` file.

Install the requirements:

```shell
sudo apt-get update
sudo apt-get install portaudio19-dev python3-pyaudio
pip install -r requirements.txt
```

To stream the video from the raspberry pi camera to the mediamtx server, run the following command on the raspberry pi:

```shell
rpicam-vid -t 0 \
  --codec h264 \
  --profile high \
  --level 4.2 \
  --framerate 25 \
  --width 1980 --height 1080 \
  --bitrate 18000000 \
  --intra 75 \
  --denoise cdn_hq \
  --inline \
  --nopreview \
  -o - | ffmpeg \
  -re -fflags +genpts \
  -f h264 -i - \
  -c:v copy \
  -rtsp_transport tcp \
  -f rtsp rtsp://raspberrypi.netbird.cloud:8554/birdcam
  ```

And in a separate session, run the birdhouse-python script to log the sensor data:

```
python3 main.py
```