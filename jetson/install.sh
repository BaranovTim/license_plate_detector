#!/usr/bin/env bash
# Runs ON the Jetson (deploy.sh calls it). Builds the image and starts the service so it
# comes back by itself after every power-on - no PC needed.
set -e
cd "$(dirname "$0")"

[ -f config.env ] || { echo "config.env missing - copy config.example.env and fill it in"; exit 1; }

# Powerbank-friendly: 5 W power mode (the Nano 2GB draws up to 10 W in MAXN mode)
sudo nvpmodel -m 1 || true

sudo mkdir -p /data/anpr
sudo docker build -t anpr-device .
sudo docker rm -f anpr-device >/dev/null 2>&1 || true
# /dev is mounted + a cgroup rule for USB serial (major 166) so re-plugging the camera
# (ttyACM0 -> ttyACM1) doesn't need a container restart
sudo docker run -d --name anpr-device --restart unless-stopped \
  --network host --env-file config.env \
  -v /data/anpr:/data/anpr -v /dev:/dev --device-cgroup-rule='c 166:* rmw' \
  --log-opt max-size=10m --log-opt max-file=3 \
  anpr-device

echo "Started. Logs: sudo docker logs -f anpr-device"
