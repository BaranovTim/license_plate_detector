# ANPR system: XIAO ESP32-S3 Sense + Jetson Nano 2GB + Mac

```
 XIAO ESP32-S3 Sense ──USB cable──▶ Jetson Nano 2GB ──Wi-Fi──▶ Mac (control server + web UI)
 camera, powered by the Jetson      capture → disk → AI        users & roles, allowed plates,
                                    powered by a powerbank      events, review, training data
```

| Folder | Runs on | What |
|---|---|---|
| `esp32/usb_camera/` | XIAO ESP32-S3 Sense | Firmware: the board becomes a USB camera (`SNAP` → JPEG) |
| `esp32/test_camera.py` | Mac | Takes one test photo over USB |
| `jetson/` | Jetson Nano (Docker) | Capture, motion detection, recognition, file handling, upload |
| `mac/` | Mac | Flask server + SQLite: users/roles, allowlist, events, review, training export |
| `deploy.sh` | Mac | Pushes `jetson/` to the Jetson and (re)starts it |

How it works, and the reasoning behind disk space, energy, speed and training: [DESIGN.md](DESIGN.md).

---

## 1 · Mac: control server

```bash
cd ~/Desktop/projects/anpr-system/mac && uv venv && uv pip install -r requirements.txt
```
```bash
.venv/bin/python server.py create-user admin --role admin
```
```bash
.venv/bin/python server.py add-device gate-1
```
Copy the printed `DEVICE_ID` / `DEVICE_TOKEN` lines into `jetson/config.env` (step 3).
```bash
.venv/bin/python server.py run
```
Open http://localhost:8080 and log in. If macOS asks whether Python may accept incoming connections, click **Allow**,
because the Jetson connects to it over Wi-Fi. Find the Mac's address for the Jetson with:
```bash
ipconfig getifaddr en0
```

## 2 · ESP32: flash the camera firmware (once, from the Mac)

Plug the XIAO into the Mac with a USB-C data cable.
```bash
arduino-cli compile --upload -p /dev/cu.usbmodem101 -b esp32:esp32:XIAO_ESP32S3:PSRAM=opi esp32/usb_camera
```
If the upload can't connect: hold **BOOT** (the small button by the USB port), plug the cable in, release, and upload again.

Test it, which saves `test_photo.jpg`:
```bash
mac/.venv/bin/pip install pyserial && mac/.venv/bin/python esp32/test_camera.py
```

## 3 · Jetson Nano 2GB: one-time setup

1. Flash **JetPack 4.6.x** (Jetson Nano 2GB SD card image) to a microSD (32 GB+) and complete the first-boot setup.
2. **Wi-Fi:** the Nano 2GB has no built-in Wi-Fi, so plug in a USB Wi-Fi adapter (some kits include one), then:
   `sudo nmcli device wifi connect "<SSID>" password "<password>"`
3. Note its address (`hostname -I`). SSH is enabled by default.
4. Optional, saves RAM and power: `sudo systemctl set-default multi-user.target` (boot without the desktop).

On the Mac:
```bash
cp jetson/config.example.env jetson/config.env
```
Edit `jetson/config.env`: `SERVER_URL=http://<mac-ip>:8080` plus the `DEVICE_ID` / `DEVICE_TOKEN` from step 1. Then:
```bash
./deploy.sh <user>@<jetson-ip>
```
This copies the code, builds the Docker image (the first build downloads ~200 MB) and starts the service with
`--restart unless-stopped`, so it starts by itself on every power-on. Run `./deploy.sh` again after any code change.

## 4 · Run

Plug the XIAO into the Jetson's USB port and power the Jetson from the powerbank (USB-C, **5 V / 3 A**).
No PC is needed after that: the dashboard on the Mac shows the device online, its disk/queue/temperature,
and every plate read. Allowed or denied is decided against the **Allowed plates** list.

Logs on the Jetson: `sudo docker logs -f anpr-device`

## Test without hardware

The device service runs on the Mac too, replaying a folder of images in place of the camera:
```bash
cd jetson && CAMERA=folder:/path/to/images DATA_DIR=/tmp/anpr SERVER_URL=http://127.0.0.1:8080 DEVICE_TOKEN=<token> ../mac/.venv/bin/python anpr_device.py
```
(needs `uv pip install -r jetson/requirements.txt` in that venv first)
