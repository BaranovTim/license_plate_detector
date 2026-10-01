# ANPR system design

## 1. Components and data flow

| Component | Hardware | Role |
|---|---|---|
| Camera | Seeed XIAO ESP32-S3 Sense (OV3660 sensor, 8 MB PSRAM) | Captures JPEGs on request over USB. No Wi-Fi, no image logic, so it draws little power and stays simple. |
| Edge device | Jetson Nano 2GB, powerbank, USB Wi-Fi | Motion detection, writes images to disk, recognition, file lifecycle, offline buffering |
| Control server | Mac | Users and roles, allowed-plates database, event history, review of failures, training data |

```
camera ──SNAP/JPEG──▶ capture thread ──motion?──▶ incoming/<event>_<n>.jpg
                                                       │ (read back from disk)
                                                 recognizer thread (fast-alpr)
                                       success ◀───────┴───────▶ failure
                         images deleted, event queued      best frame renamed
                                                            <event>_<n>__<reason>__<guess>_<conf>.jpg
                                                            → unrecognized/, event queued
                                           outbox.db (SQLite) ──HTTP──▶ Mac /api/events → decision
```

The Jetson polls the camera at `POLL_FPS` (2/s). Each frame is shrunk to 160×120 grey and compared with a running
background. If more than `MOTION_THRESHOLD` (2 %) of pixels changed, a burst of `BURST` (3) full-resolution images,
0.4 s apart, is written to `incoming/`. Writes go to a `.tmp` file and are renamed, so the recognizer never opens a half-written image.

## 2. Recognition

| Stage | Model | Size |
|---|---|---|
| Plate detector | YOLOv9-tiny, 384 px input, ONNX (`open-image-models`) | 7.4 MB |
| Plate OCR | CCT-XS v2 "global" model, ONNX (`fast-plate-ocr`) | 3.2 MB |

Both run with ONNX Runtime on the Nano's CPU, so CUDA/TensorRT isn't needed. That keeps the power draw and the
2 GB of RAM free for the rest of the system. The OCR returns a confidence per character, and a read counts as
**successful** only when:

- a plate was detected (otherwise reason `no_plate`),
- the text has ≥ 4 characters (`unreadable`),
- the **weakest character** reaches `MIN_CONFIDENCE` = 0.80 (`low_confidence`),
- and it matches `PLATE_REGEX` if one is set, e.g. `^[A-Z]{3}[0-9]{3}$` for Lithuanian plates (`bad_format`).

Within a burst, processing stops at the first successful frame. The same plate seen again within `DEDUPE_SECONDS`
(30 s) is not reported twice: a car standing at the barrier causes motion more than once.

## 3. Folders and file lifecycle (on the Jetson, `/data/anpr`)

| Path | Content | Lifetime |
|---|---|---|
| `incoming/` | Every captured image: `20261001_133515_966_0.jpg` (event time + frame number) | Seconds, until recognized |
| `unrecognized/` | Failed events, renamed: `…_1__low_confidence__ABC12_0.61.jpg` (reason, best guess, confidence) | Until the quota is reached, then oldest first |
| `outbox.db` | Events not yet confirmed by the Mac | Until delivered |
| `allowlist.json` | Copy of the allowed plates, refreshed every 5 min | Used when the Mac is unreachable |

- **Success:** all images of the event are **deleted**. Only the event (plate, confidence, small plate crop) goes to the Mac.
- **Failure:** the most useful frame is **renamed and moved** to `unrecognized/`, and the near-identical burst frames are
  deleted. Set `KEEP_ALL_FAILED_FRAMES=true` to keep the whole burst. The frame is also uploaded to the Mac for review.
- After a power cut, leftover `.tmp` files are removed at start-up. Unprocessed `incoming/` images are processed, and
  `outbox.db` is re-sent.

## 4. Users, roles and database (Mac, `mac/data/anpr.db`)

| Permission | admin | operator | viewer |
|---|:-:|:-:|:-:|
| Dashboard, events, allowed plates (read) | ✓ | ✓ | ✓ |
| Add / remove allowed plates | ✓ | ✓ | |
| Review unrecognized images, export training data | ✓ | ✓ | |
| Manage users and roles, register devices, audit log | ✓ | | |

Tables:

- `users`: username, password hash (werkzeug PBKDF2), role, active.
- `allowed_plates`: plate, owner, note, `valid_until` (temporary access), who added it.
- `devices`: device id, **SHA-256 hash** of its API token (never the token itself), last heartbeat and its JSON status.
- `events`: device, capture/receive time, `recognized`/`unrecognized`, plate, confidence, region, failure reason,
  decision (`allowed`/`denied`/`unknown`), processing time, image paths, and review fields (corrected plate, reviewer, time).
  `UNIQUE(device_id, device_event_id)` makes retries after network errors harmless.
- `audit_log`: logins and every change (plates, users, reviews, exports, device tokens).

Web forms are protected by a per-session CSRF token. Devices authenticate with `Authorization: Bearer <token>`.

**Decision:** a plate is *allowed* if it's in `allowed_plates` and not expired; otherwise it's *denied*. When the Mac is
unreachable, the Jetson decides from its cached `allowlist.json` (`on_decision` hook) and sends the event later.

## 5. Disk space

These assumptions should be measured on site; the dashboard shows the real numbers.

| Item | Estimate |
|---|---|
| XGA (1024×768) JPEG, quality 12 | **33 KB measured** indoors (OV3660); assume ≈ 60 KB outdoors, where scenes have more detail |
| Traffic | 300 cars/day, 20 % failed reads |
| `incoming/` | 3 × 60 KB per event, held for ~1 s, so effectively 0 |
| `unrecognized/` | 60 failed × 1 frame × 60 KB ≈ **3.6 MB/day**, so the 1 GB quota (`MAX_UNRECOGNIZED_MB`) holds ~9 months |
| Without deletion | 300 × 3 × 60 KB ≈ 54 MB/day ≈ 20 GB/year, which wouldn't fit on a 32 GB card next to the OS |
| Mac | Plate crops ≈ 5 KB × 300/day, kept `RETENTION_DAYS` = 30, so ≈ 45 MB. Failed full frames are kept until reviewed. |

Safety rails: when free space drops below `MIN_FREE_MB` (500 MB), capture pauses and the dashboard shows it. The
housekeeping thread deletes the oldest `unrecognized/` images once over quota. Docker logs are capped at 3 × 10 MB.

## 6. Energy (powerbank)

| Consumer | Estimate |
|---|---|
| Jetson Nano 2GB, 5 W mode (`nvpmodel -m 1`), headless, CPU-only inference | 2.5–3.5 W |
| XIAO ESP32-S3 Sense, camera on, Wi-Fi off | 0.5–0.8 W |
| USB Wi-Fi adapter | ~0.5 W |
| **Total** | **≈ 4 W** |

A 20 000 mAh powerbank holds 20 Ah × 3.7 V = 74 Wh, or about 63 Wh at the 5 V output after conversion losses.
That gives 63 Wh / 4 W ≈ **15 hours**. Measure with a USB power meter to confirm.

Choices that save energy:
- Motion-triggered recognition: the AI runs only when something moves; idle work is one small frame comparison every 0.5 s.
- The camera has Wi-Fi off and gets its power and data over a single USB cable.
- The GPU is unused, the 5 W power mode is set, and the desktop can be disabled.
- `POLL_FPS`, `FRAME_SIZE` and `BURST` trade reaction time for power.

## 7. Speed

| Step | Time |
|---|---|
| Motion noticed | ≤ 0.5 s (2 frames/s) |
| XGA JPEG over USB (full speed, 12 Mbit/s) | **40 ms measured** for 33 KB (≈ 830 KB/s) |
| Recognition per image | 8–20 ms measured on the Mac (Apple Silicon). Estimated 200–500 ms on the Nano's Cortex-A57 CPU. The dashboard shows the measured average. |
| Decision from the Mac | one HTTP round trip on the LAN (~10–50 ms), or ~1 ms from the offline cache |

Total: a car is identified about **1 second** after it enters the frame.

## 8. Training with unsuccessful images

1. Failed frames reach the Mac's **Review** page, with the reason, the best guess and the plate crop if one was found.
2. An operator types the real plate number, or marks the frame as **No plate** (e.g. a pedestrian triggered motion).
3. Labeled images go to the training set:
   - `training/ocr/`: plate crops and `labels.csv` (`image_path,plate_text`), the format of the `fast-plate-ocr`
     training CLI, for fine-tuning the OCR on local plates.
   - `training/detector/`: full frames where no plate was detected. They need boxes drawn (Roboflow / CVAT) before
     YOLO fine-tuning.
4. **Training data → Download** exports a zip. Train on the Mac or in Colab, export to ONNX, copy the files to the Jetson
   and select them with `OCR_MODEL` / `DETECTOR_MODEL`.
5. The **Training data** page counts failure reasons, so it's clear what to fix: many `no_plate` means the camera
   angle or the detector; many `low_confidence` means OCR training, lighting or resolution.
