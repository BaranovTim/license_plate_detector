#!/usr/bin/env python3
"""
ANPR control server - runs on the Mac.

  Web UI   http://localhost:8080      (login, roles: admin / operator / viewer)
  Device   POST /api/events           recognized / unrecognized events from the Jetson
           POST /api/heartbeat        device health
           GET  /api/allowlist        allowed plates (the Jetson caches it for offline decisions)

CLI:
  python server.py create-user <name> --role admin|operator|viewer
  python server.py add-device <device-id>        prints the device token for jetson/config.env
  python server.py run [--port 8080]
"""
import argparse
import csv
import getpass
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import zipfile
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ANPR_DATA", BASE / "data"))
DB_PATH = DATA / "anpr.db"
IMAGES = DATA / "images"
TRAINING = DATA / "training"
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", 30))  # crops of recognized plates

ROLES = ("admin", "operator", "viewer")
# what each role may do
PERMISSIONS = {
    "view": {"admin", "operator", "viewer"},
    "review": {"admin", "operator"},
    "edit_plates": {"admin", "operator"},
    "manage_users": {"admin"},
    "manage_devices": {"admin"},
    "audit": {"admin"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'viewer')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allowed_plates (
    id INTEGER PRIMARY KEY,
    plate TEXT UNIQUE NOT NULL,
    owner TEXT,
    note TEXT,
    valid_until TEXT,               -- NULL = no expiry
    created_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    last_seen TEXT,
    status TEXT                     -- last heartbeat JSON
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL,
    device_event_id TEXT,
    captured_at TEXT,
    received_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('recognized', 'unrecognized')),
    plate TEXT,
    confidence REAL,
    region TEXT,
    reason TEXT,
    decision TEXT,                  -- allowed / denied / unknown
    processing_ms INTEGER,
    crop_path TEXT,
    image_path TEXT,
    review_status TEXT,             -- NULL / labeled / discarded (unrecognized events only)
    corrected_plate TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    UNIQUE (device_id, device_event_id)
);
CREATE INDEX IF NOT EXISTS events_received ON events (received_at);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    username TEXT,
    action TEXT NOT NULL,
    details TEXT
);
"""

app = Flask(__name__)


# ======================================================================= helpers

def now():
    return datetime.now().isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_storage():
    for d in (DATA, IMAGES, TRAINING / "ocr", TRAINING / "detector"):
        d.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.close()
    key_file = DATA / "secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
    app.secret_key = key_file.read_text().strip()


def normalize_plate(text):
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def audit(action, details=""):
    get_db().execute("INSERT INTO audit_log (at, username, action, details) VALUES (?, ?, ?, ?)",
                     (now(), session.get("username"), action, details))
    get_db().commit()


def current_allowlist(db):
    today = date.today().isoformat()
    rows = db.execute("SELECT plate FROM allowed_plates WHERE valid_until IS NULL OR valid_until >= ?",
                      (today,)).fetchall()
    return [r["plate"] for r in rows]


def decide(db, plate):
    if not plate:
        return "unknown", None
    today = date.today().isoformat()
    row = db.execute("SELECT owner FROM allowed_plates WHERE plate = ? AND (valid_until IS NULL OR valid_until >= ?)",
                     (plate, today)).fetchone()
    return ("allowed", row["owner"]) if row else ("denied", None)


def save_upload(file_storage, subdir, name):
    folder = IMAGES / subdir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    file_storage.save(path)
    return str(path.relative_to(IMAGES))


def cleanup_old_crops(db):
    """Recognized-plate crops are only evidence; drop them after RETENTION_DAYS."""
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat()
    for row in db.execute("SELECT id, crop_path FROM events WHERE kind = 'recognized' AND received_at < ? "
                          "AND crop_path IS NOT NULL", (cutoff,)).fetchall():
        (IMAGES / row["crop_path"]).unlink(missing_ok=True)
        db.execute("UPDATE events SET crop_path = NULL WHERE id = ?", (row["id"],))
    db.commit()


# ======================================================================= auth

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    if request.method == "POST" and not request.path.startswith("/api/"):
        if request.form.get("csrf") != session.get("csrf"):
            abort(400, "CSRF token missing or wrong - reload the page")


def can(permission):
    return session.get("role") in PERMISSIONS[permission]


app.jinja_env.globals["can"] = can


def requires(permission):
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if "username" not in session:
                return redirect(url_for("login", next=request.path))
            if not can(permission):
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return deco


def device_auth(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = get_db().execute("SELECT id FROM devices WHERE token_hash = ?", (token_hash,)).fetchone()
        if not token or row is None:
            return jsonify(error="invalid device token"), 401
        g.device_id = row["id"]
        return view(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = get_db().execute("SELECT * FROM users WHERE username = ? AND active = 1",
                                (request.form["username"],)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form["password"]):
            session.clear()
            session.update(username=user["username"], role=user["role"])
            audit("login")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))
        flash("Wrong username or password")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ======================================================================= device API

@app.post("/api/events")
@device_auth
def api_events():
    db = get_db()
    try:
        meta = json.loads(request.form["meta"])
        kind = meta["kind"]
        assert kind in ("recognized", "unrecognized")
    except (KeyError, ValueError, AssertionError):
        return jsonify(error="bad meta"), 400

    plate = normalize_plate(meta.get("plate"))
    event_id = meta.get("event_id")
    dup = db.execute("SELECT decision FROM events WHERE device_id = ? AND device_event_id = ?",
                     (g.device_id, event_id)).fetchone()
    if dup:  # the device retried after a network error - already stored
        return jsonify(decision=dup["decision"], duplicate=True)

    decision, owner = decide(db, plate) if kind == "recognized" else ("unknown", None)
    day = date.today().isoformat()
    crop_path = image_path = None
    if "crop" in request.files:
        crop_path = save_upload(request.files["crop"], f"{day}", f"{g.device_id}_{event_id}_crop.jpg")
    if "image" in request.files:
        image_path = save_upload(request.files["image"], f"{day}", f"{g.device_id}_{event_id}_full.jpg")

    db.execute("""INSERT INTO events (device_id, device_event_id, captured_at, received_at, kind, plate, confidence,
                  region, reason, decision, processing_ms, crop_path, image_path)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (g.device_id, event_id, meta.get("captured_at"), now(), kind, plate or None, meta.get("confidence"),
                meta.get("region"), meta.get("reason"), decision, meta.get("processing_ms"), crop_path, image_path))
    db.commit()
    return jsonify(decision=decision, owner=owner)


@app.post("/api/heartbeat")
@device_auth
def api_heartbeat():
    db = get_db()
    db.execute("UPDATE devices SET last_seen = ?, status = ? WHERE id = ?",
               (now(), json.dumps(request.get_json(silent=True) or {}), g.device_id))
    db.commit()
    cleanup_old_crops(db)
    return jsonify(ok=True)


@app.get("/api/allowlist")
@device_auth
def api_allowlist():
    return jsonify(plates=current_allowlist(get_db()))


# ======================================================================= web UI

@app.route("/")
@requires("view")
def dashboard():
    db = get_db()
    today = date.today().isoformat()
    stats = db.execute("""SELECT
            SUM(kind = 'recognized') AS recognized,
            SUM(kind = 'unrecognized') AS unrecognized,
            SUM(decision = 'allowed') AS allowed,
            SUM(decision = 'denied') AS denied,
            ROUND(AVG(processing_ms)) AS avg_ms
        FROM events WHERE received_at >= ?""", (today,)).fetchone()
    devices = []
    for d in db.execute("SELECT * FROM devices ORDER BY id").fetchall():
        status = json.loads(d["status"] or "{}")
        online = bool(d["last_seen"]) and datetime.fromisoformat(d["last_seen"]) > datetime.now() - timedelta(seconds=90)
        devices.append({"id": d["id"], "last_seen": d["last_seen"], "online": online, "s": status})
    events = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 15").fetchall()
    to_review = db.execute("SELECT COUNT(*) FROM events WHERE kind = 'unrecognized' AND review_status IS NULL").fetchone()[0]
    return render_template("dashboard.html", stats=stats, devices=devices, events=events, to_review=to_review)


@app.route("/events")
@requires("view")
def events():
    q = normalize_plate(request.args.get("q"))
    kind = request.args.get("kind", "")
    sql, args = "SELECT * FROM events WHERE 1=1", []
    if q:
        sql += " AND (plate LIKE ? OR corrected_plate LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    if kind in ("recognized", "unrecognized", "allowed", "denied"):
        sql += " AND (kind = ? OR decision = ?)"
        args += [kind, kind]
    rows = get_db().execute(sql + " ORDER BY id DESC LIMIT 200", args).fetchall()
    return render_template("events.html", events=rows, q=q, kind=kind)


@app.route("/images/<path:name>")
@requires("view")
def image(name):
    return send_from_directory(IMAGES, name)


@app.route("/review")
@requires("review")
def review():
    rows = get_db().execute("SELECT * FROM events WHERE kind = 'unrecognized' AND review_status IS NULL "
                            "ORDER BY id LIMIT 30").fetchall()
    return render_template("review.html", events=rows)


@app.post("/review/<int:event_id>")
@requires("review")
def review_save(event_id):
    db = get_db()
    ev = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if ev is None:
        abort(404)
    if request.form.get("action") == "discard":  # e.g. motion without a car
        for p in (ev["crop_path"], ev["image_path"]):
            if p:
                (IMAGES / p).unlink(missing_ok=True)
        db.execute("UPDATE events SET review_status = 'discarded', crop_path = NULL, image_path = NULL, "
                   "reviewed_by = ?, reviewed_at = ? WHERE id = ?", (session["username"], now(), event_id))
        db.commit()
        audit("review_discard", f"event {event_id}")
        return redirect(url_for("review"))

    plate = normalize_plate(request.form.get("plate"))
    if len(plate) < 2:
        flash("Enter the plate number you can see")
        return redirect(url_for("review"))
    # Training data: plate crops -> OCR dataset, full images where no plate was found -> detector dataset
    if ev["crop_path"]:
        target = TRAINING / "ocr" / f"{event_id}_{plate}.jpg"
        target.write_bytes((IMAGES / ev["crop_path"]).read_bytes())
        labels = TRAINING / "ocr" / "labels.csv"
        new_file = not labels.exists()
        with open(labels, "a", newline="") as f:
            if new_file:
                csv.writer(f).writerow(["image_path", "plate_text"])
            csv.writer(f).writerow([target.name, plate])
    elif ev["image_path"]:
        target = TRAINING / "detector" / f"{event_id}_{plate}.jpg"
        target.write_bytes((IMAGES / ev["image_path"]).read_bytes())
    decision, _ = decide(db, plate)
    db.execute("UPDATE events SET review_status = 'labeled', corrected_plate = ?, decision = ?, reviewed_by = ?, "
               "reviewed_at = ? WHERE id = ?", (plate, decision, session["username"], now(), event_id))
    db.commit()
    audit("review_label", f"event {event_id} -> {plate}")
    flash(f"Saved {plate} for training ({decision})")
    return redirect(url_for("review"))


@app.route("/training")
@requires("review")
def training():
    ocr = list((TRAINING / "ocr").glob("*.jpg"))
    det = list((TRAINING / "detector").glob("*.jpg"))
    reasons = get_db().execute("SELECT reason, COUNT(*) AS n FROM events WHERE kind = 'unrecognized' "
                               "GROUP BY reason ORDER BY n DESC").fetchall()
    return render_template("training.html", n_ocr=len(ocr), n_det=len(det), reasons=reasons)


@app.route("/training/export.zip")
@requires("review")
def training_export():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in TRAINING.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(TRAINING))
    buf.seek(0)
    audit("training_export")
    return send_file(buf, download_name=f"anpr_training_{date.today()}.zip", as_attachment=True)


@app.route("/plates", methods=["GET", "POST"])
@requires("view")
def plates():
    db = get_db()
    if request.method == "POST":
        if not can("edit_plates"):
            abort(403)
        if request.form.get("action") == "delete":
            plate = request.form["plate"]
            db.execute("DELETE FROM allowed_plates WHERE plate = ?", (plate,))
            db.commit()
            audit("plate_delete", plate)
            flash(f"Removed {plate}")
        else:
            plate = normalize_plate(request.form.get("plate"))
            if len(plate) < 2:
                flash("Plate number is required")
            else:
                db.execute("""INSERT INTO allowed_plates (plate, owner, note, valid_until, created_by, created_at)
                              VALUES (?, ?, ?, ?, ?, ?)
                              ON CONFLICT(plate) DO UPDATE SET owner = excluded.owner, note = excluded.note,
                              valid_until = excluded.valid_until""",
                           (plate, request.form.get("owner"), request.form.get("note"),
                            request.form.get("valid_until") or None, session["username"], now()))
                db.commit()
                audit("plate_save", plate)
                flash(f"Saved {plate}")
        return redirect(url_for("plates"))
    rows = db.execute("SELECT * FROM allowed_plates ORDER BY plate").fetchall()
    return render_template("plates.html", plates=rows, today=date.today().isoformat())


@app.route("/users", methods=["GET", "POST"])
@requires("manage_users")
def users():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        username = request.form.get("username", "").strip()
        if action == "create":
            role, password = request.form.get("role"), request.form.get("password", "")
            if role not in ROLES or len(username) < 2 or len(password) < 8:
                flash("Username, a role and a password of 8+ characters are required")
            else:
                try:
                    db.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                               (username, generate_password_hash(password), role, now()))
                    db.commit()
                    audit("user_create", f"{username} ({role})")
                    flash(f"Created {username}")
                except sqlite3.IntegrityError:
                    flash("That username exists")
        elif action == "toggle" and username != session["username"]:
            db.execute("UPDATE users SET active = 1 - active WHERE username = ?", (username,))
            db.commit()
            audit("user_toggle", username)
        elif action == "role" and username != session["username"] and request.form.get("role") in ROLES:
            db.execute("UPDATE users SET role = ? WHERE username = ?", (request.form["role"], username))
            db.commit()
            audit("user_role", f"{username} -> {request.form['role']}")
        return redirect(url_for("users"))
    rows = db.execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("users.html", users=rows, roles=ROLES)


@app.route("/devices", methods=["GET", "POST"])
@requires("manage_devices")
def devices():
    db = get_db()
    new_token = None
    if request.method == "POST":
        device_id = re.sub(r"[^a-zA-Z0-9_-]", "", request.form.get("device_id", ""))
        if device_id:
            new_token = register_device(db, device_id)
            audit("device_token", device_id)
    rows = db.execute("SELECT * FROM devices ORDER BY id").fetchall()
    return render_template("devices.html", devices=rows, new_token=new_token)


@app.route("/audit")
@requires("audit")
def audit_view():
    rows = get_db().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300").fetchall()
    return render_template("audit.html", rows=rows)


@app.errorhandler(403)
def forbidden(_e):
    return render_template("message.html", title="Not allowed",
                           text=f"Your role ({session.get('role')}) can't open this page."), 403


# ======================================================================= CLI

def register_device(db, device_id):
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO devices (id, token_hash) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET token_hash = excluded.token_hash",
               (device_id, hashlib.sha256(token.encode()).hexdigest()))
    db.commit()
    return token


def main():
    p = argparse.ArgumentParser(description="ANPR control server")
    sub = p.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user")
    cu.add_argument("username")
    cu.add_argument("--role", choices=ROLES, required=True)
    ad = sub.add_parser("add-device")
    ad.add_argument("device_id")
    run = sub.add_parser("run")
    run.add_argument("--host", default="0.0.0.0")
    run.add_argument("--port", type=int, default=8080)
    args = p.parse_args()

    init_storage()
    db = sqlite3.connect(DB_PATH)
    if args.cmd == "create-user":
        password = os.environ.get("ANPR_PASSWORD") or getpass.getpass(f"Password for {args.username}: ")
        if len(password) < 8:
            sys.exit("Password must be at least 8 characters")
        db.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash, role = excluded.role",
                   (args.username, generate_password_hash(password), args.role, now()))
        db.commit()
        print(f"User {args.username} ({args.role}) saved")
    elif args.cmd == "add-device":
        token = register_device(db, args.device_id)
        print(f"Device {args.device_id} registered. Put this in jetson/config.env:\n\nDEVICE_ID={args.device_id}\n"
              f"DEVICE_TOKEN={token}\n")
    elif args.cmd == "run":
        if not db.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
            sys.exit("No admin yet: python server.py create-user admin --role admin")
        app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
