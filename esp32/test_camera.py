#!/usr/bin/env python3
"""Take one photo from the XIAO ESP32S3 Sense (running usb_camera) over USB and save it.

    python esp32/test_camera.py                 # auto-detect port, XGA
    python esp32/test_camera.py --size UXGA -o photo.jpg
"""
import argparse
import glob
import time

import serial


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", default=None)
    p.add_argument("--size", default="XGA")
    p.add_argument("-o", "--out", default="test_photo.jpg")
    args = p.parse_args()

    port = args.port or (sorted(glob.glob("/dev/cu.usbmodem*")) + sorted(glob.glob("/dev/ttyACM*")) or [None])[0]
    if not port:
        raise SystemExit("No camera found - is the XIAO plugged in?")

    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, 115200, 3
    ser.dtr = ser.rts = False  # don't reset the board
    ser.open()
    time.sleep(0.3)
    ser.reset_input_buffer()

    def cmd(text):
        ser.write((text + "\n").encode())
        line = ser.readline().decode(errors="replace").strip()
        while line.startswith("READY"):
            line = ser.readline().decode(errors="replace").strip()
        return line

    print("port :", port)
    print("ping :", cmd("PING") or "(no answer - is esp32/usb_camera flashed?)")
    print("info :", cmd("INFO"))
    print("size :", cmd(f"SIZE {args.size}"))
    def snap():
        header = cmd("SNAP")
        if not header.startswith("JPEG "):
            raise SystemExit(f"snap failed: {header}")
        n = int(header.split()[1])
        data = ser.read(n)
        if len(data) != n or data[:2] != b"\xff\xd8":
            raise SystemExit(f"incomplete image ({len(data)}/{n} bytes)")
        return data

    for _ in range(3):  # let auto-exposure settle
        snap()
        time.sleep(0.2)

    t = time.time()
    data = snap()
    dt = time.time() - t
    n = len(data)
    with open(args.out, "wb") as f:
        f.write(data)
    print(f"photo: {args.out}  {n / 1024:.0f} KB in {dt * 1000:.0f} ms ({n / dt / 1024:.0f} KB/s over USB)")


if __name__ == "__main__":
    main()
