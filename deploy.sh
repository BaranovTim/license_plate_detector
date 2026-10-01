#!/usr/bin/env bash
# Push the device code from the Mac to the Jetson and (re)start it.
#   ./deploy.sh user@jetson-ip
set -e
TARGET=${1:?usage: ./deploy.sh user@jetson-ip}
cd "$(dirname "$0")"
[ -f jetson/config.env ] || { echo "Create jetson/config.env first (copy jetson/config.example.env)"; exit 1; }
rsync -av --delete --exclude __pycache__ jetson/ "$TARGET:anpr-device/"
ssh -t "$TARGET" "bash anpr-device/install.sh"
