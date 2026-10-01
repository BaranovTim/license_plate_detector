#!/usr/bin/env python3
"""
ANPR device service - runs on the Jetson Nano (in Docker), or on the Mac for testing.

  capture thread     camera -> motion detection -> burst of JPEGs written to  DATA_DIR/incoming/
  recognizer thread  reads images back from disk -> fast-alpr (ONNX, CPU)
                       success -> event queued, images DELETED
                       failure -> images RENAMED and MOVED to DATA_DIR/unrecognized/, event queued
  uplink thread      outbox (SQLite, survives reboots / Wi-Fi loss) -> Mac server over HTTP
                     + heartbeat with device health + allowlist cache for offline decisions
  housekeeping       disk quota for unrecognized/, low-disk protection

All settings come from environment variables (see config.example.env).
"""
import glob
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests

log = logging.getLogger("anpr")


def env(name, default, cast=str):
    value = os.environ.get(name)
    return default if value in (None, "") else cast(value)


CFG = {
    "device_id": env("DEVICE_ID", "gate-1"),
    "server_url": env("SERVER_URL", "http://192.168.1.10:8080").rstrip("/"),
    "device_token": env("DEVICE_TOKEN", ""),
    "camera": env("CAMERA", "auto"),  # auto | /dev/ttyACM0 | folder:/path/to/test/images
    "frame_size": env("FRAME_SIZE", "XGA"),
    "jpeg_quality": env("JPEG_QUALITY", 12, int),
    "data_dir": Path(env("DATA_DIR", "/data/anpr")),
    "poll_fps": env("POLL_FPS", 2.0, float),
    "motion_threshold": env("MOTION_THRESHOLD", 0.02, float),  # fraction of pixels that changed
    "burst": env("BURST", 3, int),  # images saved per motion event
    "burst_interval": env("BURST_INTERVAL", 0.4, float),
    "cooldown": env("COOLDOWN", 2.0, float),  # seconds between motion events
    "min_confidence": env("MIN_CONFIDENCE", 0.80, float),  # weakest character must reach this
    "plate_regex": env("PLATE_REGEX", ""),  # e.g. ^[A-Z]{3}[0-9]{3}$ for Lithuanian plates
    "dedupe_seconds": env("DEDUPE_SECONDS", 30, int),
    # failed events keep only their most useful frame (the rest of the burst is near-duplicate)
    "keep_all_failed_frames": env("KEEP_ALL_FAILED_FRAMES", "false").lower() == "true",
    "max_unrecognized_mb": env("MAX_UNRECOGNIZED_MB", 1000, int),
    "min_free_mb": env("MIN_FREE_MB", 500, int),
    "detector_model": env("DETECTOR_MODEL", "yolo-v9-t-384-license-plate-end2end"),
    "ocr_model": env("OCR_MODEL", "cct-xs-v2-global-model"),
}

INCOMING = CFG["data_dir"] / "incoming"
UNRECOGNIZED = CFG["data_dir"] / "unrecognized"

STATS = {
    "started": time.time(),
    "camera_ok": False,
    "camera_info": "",
    "frames": 0,
    "motion_events": 0,
    "images_saved": 0,
    "recognized": 0,
    "unrecognized": 0,
    "capture_paused": False,
    "last_plate": None,
}
PROC_TIMES = deque(maxlen=50)


# ======================================================================= camera

class UsbCamera:
    """XIAO ESP32S3 Sense running esp32/usb_camera (line protocol over USB serial)."""

    def __init__(self, port, frame_size, quality):
        self.port_setting, self.frame_size, self.quality = port, frame_size, quality
        self.ser = None

    @staticmethod
    def find_port():
        ports = sorted(glob.glob("/dev/ttyACM*")) + sorted(glob.glob("/dev/cu.usbmodem*"))
        return ports[0] if ports else None

    def _cmd(self, text):
        self.ser.write((text + "\n").encode())
        deadline = time.time() + 3
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="replace").strip()
            if line and not line.startswith("READY"):
                return line
        raise IOError(f"no answer to {text!r}")

    def open(self):
        import serial
        port = self.find_port() if self.port_setting == "auto" else self.port_setting
        if not port:
            raise IOError("camera not found (no /dev/ttyACM* device)")
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout = port, 115200, 2
        ser.dtr = ser.rts = False  # toggling these resets the ESP32-S3
        ser.open()
        self.ser = ser
        time.sleep(0.2)
        ser.reset_input_buffer()
        if self._cmd("PING") != "PONG":
            raise IOError("camera did not answer PING - is esp32/usb_camera flashed?")
        for cmd in (f"SIZE {self.frame_size}", f"QUALITY {self.quality}"):
            answer = self._cmd(cmd)
            if answer != "OK":
                raise IOError(f"{cmd}: {answer}")
        STATS["camera_info"] = f"{port} {self._cmd('INFO')}"
        log.info("camera ready: %s", STATS["camera_info"])

    def snap(self):
        self.ser.write(b"SNAP\n")
        header = self.ser.readline().decode(errors="replace").strip()
        while header.startswith("READY"):
            header = self.ser.readline().decode(errors="replace").strip()
        if not header.startswith("JPEG "):
            raise IOError(f"bad snap answer: {header!r}")
        length = int(header.split()[1])
        data = self.ser.read(length)
        if len(data) != length or data[:2] != b"\xff\xd8":
            self.ser.reset_input_buffer()
            raise IOError(f"incomplete JPEG ({len(data)}/{length} bytes)")
        return data

    def close(self):
        if self.ser:
            self.ser.close()
            self.ser = None


class FolderCamera:
    """Replays images from a folder - for testing without hardware (CAMERA=folder:/path)."""

    def __init__(self, folder):
        self.files = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
        if not self.files:
            raise IOError(f"no images in {folder}")
        self.i = 0

    def open(self):
        STATS["camera_info"] = f"folder with {len(self.files)} images"

    def snap(self):
        # each file is shown for ~2 s so motion detection sees "a car arriving" then "standing"
        path = self.files[(self.i // 4) % len(self.files)]
        self.i += 1
        ok, buf = cv2.imencode(".jpg", cv2.imread(str(path)))
        return buf.tobytes()

    def close(self):
        pass


def make_camera():
    cam = CFG["camera"]
    if cam.startswith("folder:"):
        return FolderCamera(cam[len("folder:"):])
    return UsbCamera(cam, CFG["frame_size"], CFG["jpeg_quality"])


# ======================================================================= capture

def free_mb(path):
    return shutil.disk_usage(path).free / 1e6


def write_atomic(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.rename(path)  # the recognizer never sees half-written files


def capture_loop(stop):
    camera = make_camera()
    background = None
    last_event = 0.0
    period = 1.0 / CFG["poll_fps"]

    while not stop.is_set():
        try:
            camera.open()
            STATS["camera_ok"] = True
            while not stop.is_set():
                t0 = time.time()
                if free_mb(CFG["data_dir"]) < CFG["min_free_mb"]:
                    if not STATS["capture_paused"]:
                        log.warning("disk almost full - capture paused")
                    STATS["capture_paused"] = True
                    time.sleep(5)
                    continue
                STATS["capture_paused"] = False

                jpg = camera.snap()
                STATS["frames"] += 1
                frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_GRAYSCALE)
                small = cv2.GaussianBlur(cv2.resize(frame, (160, 120)), (5, 5), 0).astype(np.float32)

                if background is None:
                    background = small
                    continue
                changed = float(np.mean(cv2.absdiff(small, background) > 25))
                cv2.accumulateWeighted(small, background, 0.1)

                if changed > CFG["motion_threshold"] and t0 - last_event > CFG["cooldown"]:
                    last_event = t0
                    STATS["motion_events"] += 1
                    event_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                    log.info("motion %.1f%% -> event %s", changed * 100, event_id)
                    write_atomic(INCOMING / f"{event_id}_0.jpg", jpg)
                    for n in range(1, CFG["burst"]):
                        time.sleep(CFG["burst_interval"])
                        write_atomic(INCOMING / f"{event_id}_{n}.jpg", camera.snap())
                    STATS["images_saved"] += CFG["burst"]
                    last_event = time.time()

                time.sleep(max(0.0, period - (time.time() - t0)))
        except Exception as e:  # noqa: BLE001 - unplugged cable, serial errors, ...
            STATS["camera_ok"] = False
            log.error("camera: %s - retrying in 3 s", e)
            camera.close()
            time.sleep(3)
    camera.close()


# ======================================================================= recognition

def load_alpr():
    from fast_alpr import ALPR
    t = time.time()
    alpr = ALPR(detector_model=CFG["detector_model"], ocr_model=CFG["ocr_model"], ocr_device="cpu",
                detector_providers=["CPUExecutionProvider"])
    log.info("recognizer loaded in %.1f s", time.time() - t)
    return alpr


def read_plates(alpr, image):
    """Best plate in the image: dict(text, confidence, det_conf, region, crop) or None if no plate."""
    best = None
    for r in alpr.predict(image):
        b = r.detection.bounding_box
        crop = image[max(b.y1, 0):b.y2, max(b.x1, 0):b.x2]
        text, conf, region = "", 0.0, None
        if r.ocr is not None:
            text = re.sub(r"[^A-Z0-9]", "", r.ocr.text.upper())
            c = r.ocr.confidence
            if isinstance(c, list):  # per-character confidences: the weakest real character decides
                conf = float(min(c[:len(text)])) if text else 0.0
            else:
                conf = float(c)
            region = r.ocr.region
        cand = {"text": text, "confidence": conf, "det_conf": float(r.detection.confidence),
                "region": region, "crop": crop}
        if best is None or cand["confidence"] > best["confidence"]:
            best = cand
    return best


def judge(result):
    """(success, reason)"""
    if result is None:
        return False, "no_plate"
    if len(result["text"]) < 4:
        return False, "unreadable"
    if result["confidence"] < CFG["min_confidence"]:
        return False, "low_confidence"
    if CFG["plate_regex"] and not re.match(CFG["plate_regex"], result["text"]):
        return False, "bad_format"
    return True, "ok"


def complete_events():
    """Group incoming/ files by event id; return events whose burst is finished, oldest first."""
    events = {}
    for p in INCOMING.glob("*.jpg"):
        events.setdefault(p.stem.rsplit("_", 1)[0], []).append(p)
    burst_time = CFG["burst"] * CFG["burst_interval"] + 2
    ready = []
    for event_id, files in sorted(events.items()):
        newest = max(f.stat().st_mtime for f in files)
        if len(files) >= CFG["burst"] or time.time() - newest > burst_time:
            ready.append((event_id, sorted(files)))
    return ready


def recognize_loop(stop, outbox):
    alpr = load_alpr()
    recent = {}  # plate -> time it was last reported (de-duplication)

    while not stop.is_set():
        events = complete_events()
        if not events:
            time.sleep(0.3)
            continue
        for event_id, files in events:
            try:
                process_event(alpr, event_id, files, outbox, recent)
            except Exception:  # noqa: BLE001 - never let one bad image stop the recognizer
                log.exception("event %s failed - moving its images to unrecognized/", event_id)
                for f in files:
                    if f.exists():
                        f.rename(UNRECOGNIZED / f"{f.stem}__error__NA_0.00.jpg")


def process_event(alpr, event_id, files, outbox, recent):
    t0 = time.time()
    best, best_file = None, None
    for f in files:
        image = cv2.imread(str(f))  # read back from disk, as the assignment requires
        if image is None:
            continue
        res = read_plates(alpr, image)
        if res and (best is None or res["confidence"] > best["confidence"]):
            best, best_file = res, f
        if res and judge(res)[0]:
            break  # good enough, skip the rest of the burst
    ms = (time.time() - t0) * 1000 / max(len(files), 1)
    PROC_TIMES.append(ms)
    success, reason = judge(best)
    captured_at = datetime.strptime(event_id, "%Y%m%d_%H%M%S_%f").isoformat(timespec="seconds")

    meta = {"event_id": event_id, "captured_at": captured_at, "device_id": CFG["device_id"],
            "processing_ms": round(ms), "images": len(files)}
    crop_jpg = None
    if best is not None and best["crop"].size:
        crop_jpg = cv2.imencode(".jpg", best["crop"])[1].tobytes()
        meta.update(plate=best["text"], confidence=round(best["confidence"], 3),
                    det_conf=round(best["det_conf"], 3), region=best["region"])

    if success:
        STATS["recognized"] += 1
        STATS["last_plate"] = best["text"]
        for f in files:
            f.unlink(missing_ok=True)  # success -> images deleted
        if time.time() - recent.get(best["text"], 0) < CFG["dedupe_seconds"]:
            log.info("%s again (duplicate within %ss, not reported)", best["text"], CFG["dedupe_seconds"])
            return
        recent[best["text"]] = time.time()
        meta["kind"] = "recognized"
        log.info("RECOGNIZED %s (conf %.2f, %.0f ms/image)", best["text"], best["confidence"], ms)
        outbox.add(meta, crop=crop_jpg)
    else:
        STATS["unrecognized"] += 1
        # failure -> rename with the reason and best guess, move to unrecognized/
        guess = (best or {}).get("text") or "NA"
        conf = (best or {}).get("confidence", 0.0)
        main_file = best_file if best_file in files else files[len(files) // 2]
        main_image = None
        for f in files:
            if f == main_file or CFG["keep_all_failed_frames"]:
                target = UNRECOGNIZED / f"{f.stem}__{reason}__{guess}_{conf:.2f}.jpg"
                f.rename(target)
                if f == main_file:
                    main_image = target
            else:
                f.unlink(missing_ok=True)  # near-duplicate burst frame
        meta.update(kind="unrecognized", reason=reason)
        log.info("UNRECOGNIZED event %s: %s (guess %s, conf %.2f)", event_id, reason, guess, conf)
        outbox.add(meta, crop=crop_jpg, image_path=str(main_image))


# ======================================================================= uplink

class Outbox:
    """Durable queue of events waiting to be sent to the Mac (SQLite)."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.wake = threading.Event()  # set when a new event arrives, so the uplink sends it at once
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, meta TEXT, crop BLOB, "
                        "image_path TEXT, attempts INTEGER DEFAULT 0)")
        self.db.commit()

    def add(self, meta, crop=None, image_path=None):
        with self.lock:
            self.db.execute("INSERT INTO outbox (meta, crop, image_path) VALUES (?, ?, ?)",
                            (json.dumps(meta), crop, image_path))
            self.db.commit()
        self.wake.set()

    def pending(self, limit=20):
        with self.lock:
            return self.db.execute("SELECT id, meta, crop, image_path FROM outbox ORDER BY id LIMIT ?",
                                   (limit,)).fetchall()

    def count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def done(self, row_id):
        with self.lock:
            self.db.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
            self.db.commit()


def cpu_temp():
    temps = []
    for zone in glob.glob("/sys/devices/virtual/thermal/thermal_zone*/temp"):
        try:
            temps.append(int(Path(zone).read_text()) / 1000)
        except (OSError, ValueError):
            pass
    return max(temps) if temps else None


def dir_mb(path):
    return sum(f.stat().st_size for f in path.glob("*") if f.is_file()) / 1e6


def health():
    return {
        "device_id": CFG["device_id"],
        "uptime_s": int(time.time() - STATS["started"]),
        "camera_ok": STATS["camera_ok"],
        "camera_info": STATS["camera_info"],
        "capture_paused": STATS["capture_paused"],
        "frames": STATS["frames"],
        "motion_events": STATS["motion_events"],
        "recognized": STATS["recognized"],
        "unrecognized": STATS["unrecognized"],
        "incoming_files": len(list(INCOMING.glob("*.jpg"))),
        "unrecognized_files": len(list(UNRECOGNIZED.glob("*.jpg"))),
        "unrecognized_mb": round(dir_mb(UNRECOGNIZED), 1),
        "disk_free_mb": round(free_mb(CFG["data_dir"])),
        "avg_processing_ms": round(sum(PROC_TIMES) / len(PROC_TIMES)) if PROC_TIMES else None,
        "cpu_temp_c": cpu_temp(),
        "poll_fps": CFG["poll_fps"],
        "frame_size": CFG["frame_size"],
    }


def local_decision(plate):
    """Offline fallback: the cached allowlist from the server."""
    try:
        allowed = json.loads((CFG["data_dir"] / "allowlist.json").read_text())
        return "allowed" if plate in allowed else "denied"
    except (OSError, ValueError):
        return "unknown"


def on_decision(meta, decision, source):
    """Hook for real-world actions: open a barrier via a Jetson GPIO pin, beep, light an LED..."""
    if meta.get("kind") == "recognized":
        log.info("DECISION %s -> %s (%s)", meta.get("plate"), decision.upper(), source)


def uplink_loop(stop, outbox):
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {CFG['device_token']}"
    url = CFG["server_url"]
    next_heartbeat = next_allowlist = 0.0
    backoff = 1

    while not stop.is_set():
        now = time.time()
        try:
            if now >= next_allowlist:
                r = session.get(f"{url}/api/allowlist", timeout=5)
                r.raise_for_status()
                (CFG["data_dir"] / "allowlist.json").write_text(json.dumps(r.json()["plates"]))
                next_allowlist = now + 300
            if now >= next_heartbeat:
                h = health()
                h["outbox"] = outbox.count()
                session.post(f"{url}/api/heartbeat", json=h, timeout=5).raise_for_status()
                next_heartbeat = now + 30

            for row_id, meta_json, crop, image_path in outbox.pending():
                meta = json.loads(meta_json)
                files = {}
                if crop:
                    files["crop"] = ("crop.jpg", crop, "image/jpeg")
                if image_path and os.path.exists(image_path):
                    files["image"] = (os.path.basename(image_path), Path(image_path).read_bytes(), "image/jpeg")
                r = session.post(f"{url}/api/events", data={"meta": json.dumps(meta)}, files=files, timeout=15)
                if r.status_code in (400, 413, 422):  # the server will never accept this one - don't block the queue
                    log.error("server rejected event %s: %s %s", meta.get("event_id"), r.status_code, r.text[:200])
                    outbox.done(row_id)
                    continue
                r.raise_for_status()
                outbox.done(row_id)
                on_decision(meta, r.json().get("decision", "unknown"), "server")
            backoff = 1
        except requests.RequestException as e:
            # Mac unreachable: decide locally from the cached allowlist, keep events queued
            for row_id, meta_json, _, _ in outbox.pending():
                meta = json.loads(meta_json)
                if meta.get("kind") == "recognized" and not meta.get("local_decided"):
                    on_decision(meta, local_decision(meta.get("plate")), "offline cache")
                    meta["local_decided"] = True
                    with outbox.lock:
                        outbox.db.execute("UPDATE outbox SET meta = ? WHERE id = ?", (json.dumps(meta), row_id))
                        outbox.db.commit()
            log.warning("server unreachable (%s) - %d events queued, retry in %ds",
                        e.__class__.__name__, outbox.count(), backoff)
            outbox.wake.wait(backoff)  # a new event cuts the backoff short (offline decision right away)
            outbox.wake.clear()
            backoff = min(backoff * 2, 60)
            continue
        outbox.wake.wait(1)
        outbox.wake.clear()


# ======================================================================= housekeeping

def housekeeping_loop(stop):
    """Keep unrecognized/ under its quota by deleting the oldest images first."""
    while not stop.wait(60):
        files = sorted(UNRECOGNIZED.glob("*.jpg"), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
        limit = CFG["max_unrecognized_mb"] * 1e6
        removed = 0
        while files and (total > limit or free_mb(CFG["data_dir"]) < CFG["min_free_mb"]):
            f = files.pop(0)
            total -= f.stat().st_size
            f.unlink(missing_ok=True)
            removed += 1
        if removed:
            log.warning("housekeeping: deleted %d oldest unrecognized images (quota %d MB)",
                        removed, CFG["max_unrecognized_mb"])


# ======================================================================= main

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for d in (INCOMING, UNRECOGNIZED):
        d.mkdir(parents=True, exist_ok=True)
    for tmp in INCOMING.glob("*.tmp"):  # leftovers from a power cut
        tmp.unlink()
    if not CFG["device_token"]:
        log.warning("DEVICE_TOKEN is empty - the server will reject events")

    outbox = Outbox(CFG["data_dir"] / "outbox.db")
    stop = threading.Event()
    threads = [
        threading.Thread(target=capture_loop, args=(stop,), name="capture", daemon=True),
        threading.Thread(target=recognize_loop, args=(stop, outbox), name="recognize", daemon=True),
        threading.Thread(target=uplink_loop, args=(stop, outbox), name="uplink", daemon=True),
        threading.Thread(target=housekeeping_loop, args=(stop,), name="housekeeping", daemon=True),
    ]
    for t in threads:
        t.start()
    log.info("device %s running, server %s, data %s", CFG["device_id"], CFG["server_url"], CFG["data_dir"])
    try:
        while all(t.is_alive() for t in threads):
            time.sleep(1)
        log.error("a worker thread died - exiting so Docker restarts the service")
    except KeyboardInterrupt:
        pass
    stop.set()


if __name__ == "__main__":
    main()
