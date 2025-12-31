

```shell
sudo apt-get update
sudo apt-get install portaudio19-dev python3-pyaudio
pip install -r requirements.txt
```

```shell
rpicam-vid -t 0   --codec h264   --profile baseline   --level 4   --inline   --intra 10    --width 1296 --height 972  --bitrate 6000000   --nopreview   -o - | gst-launch-1.0 fdsrc ! h264parse config-interval=1 ! mpegtsmux ! udpsink host=raspberrypi.netbird.cloud port=1994 sync=false
```