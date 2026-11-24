#!/usr/bin/env python3
"""
tools/shop_db.py — Parameterized SQLite ledger & secure upload store for mod-shop-bench.
100% offline-capable, zero external dependencies, strict upload validation & path containment.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

ASSET_CATEGORIES = (
    "vehicle",
    "small_engine",
    "hvac",
    "generator",
    "tool",
    "appliance",
    "marine",
    "other",
)


def now_ts() -> int:
    return int(time.time())


def data_dir() -> Path:
    base = os.environ.get("MODULE_DATA_DIR")
    if base:
        p = Path(base)
    else:
        lodge = Path(os.environ.get("LODGE_DIR", Path(__file__).resolve().parents[4]))
        p = lodge / ".george" / "module_data" / "shop-bench"
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def upload_dir() -> Path:
    d = data_dir() / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def db_path() -> str:
    return str(data_dir() / "shop_bench.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'vehicle',
            make TEXT DEFAULT '',
            model TEXT DEFAULT '',
            year TEXT DEFAULT '',
            vin_or_serial TEXT DEFAULT '',
            meter_value REAL DEFAULT 0,
            meter_unit TEXT DEFAULT 'mi',
            engine_spec TEXT DEFAULT '',
            fluid_summary TEXT DEFAULT '',
            photo_path TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS service_intervals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            task_name TEXT NOT NULL,
            interval_value REAL DEFAULT 0,
            interval_unit TEXT DEFAULT 'mi',
            last_done_meter REAL DEFAULT 0,
            last_done_date TEXT DEFAULT '',
            fluid_or_part TEXT DEFAULT '',
            capacity_spec TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS torque_specs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            component TEXT NOT NULL,
            spec_value TEXT NOT NULL,
            unit TEXT DEFAULT 'ft-lb',
            fastener_size TEXT DEFAULT '',
            sequence_notes TEXT DEFAULT '',
            source TEXT DEFAULT 'manual',
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS parts_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
            part_name TEXT NOT NULL,
            oem_number TEXT DEFAULT '',
            aftermarket_xref TEXT DEFAULT '',
            category TEXT DEFAULT 'consumable',
            qty_on_hand INTEGER DEFAULT 0,
            min_qty INTEGER DEFAULT 1,
            unit_cost REAL DEFAULT 0,
            location_bin TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS maintenance_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            log_type TEXT NOT NULL DEFAULT 'service',
            title TEXT NOT NULL,
            meter_value REAL DEFAULT 0,
            summary TEXT DEFAULT '',
            checklist_json TEXT DEFAULT '[]',
            photo_path TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS obd_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
            adapter_type TEXT NOT NULL DEFAULT 'sim',
            port_or_addr TEXT DEFAULT '',
            vin TEXT DEFAULT '',
            protocol TEXT DEFAULT '',
            battery_v REAL DEFAULT 0,
            pids_json TEXT DEFAULT '{}',
            dtcs_json TEXT DEFAULT '[]',
            freeze_frame_json TEXT DEFAULT '{}',
            readiness_json TEXT DEFAULT '{}',
            raw_notes TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS can_captures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
            interface TEXT DEFAULT 'candump',
            frame_count INTEGER DEFAULT 0,
            unique_ids_json TEXT DEFAULT '[]',
            decoded_signals_json TEXT DEFAULT '[]',
            summary TEXT DEFAULT '',
            raw_sample TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS telemetry_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
            run_label TEXT NOT NULL DEFAULT 'Live Telemetry Run',
            transport TEXT NOT NULL DEFAULT 'sim',
            port_or_addr TEXT DEFAULT '',
            vehicle_year TEXT DEFAULT '',
            vehicle_make TEXT DEFAULT '',
            vehicle_model TEXT DEFAULT '',
            vin TEXT DEFAULT '',
            sim_profile TEXT DEFAULT '',
            started_at REAL NOT NULL,
            ended_at REAL DEFAULT 0,
            sample_count INTEGER DEFAULT 0,
            active_dtcs_json TEXT DEFAULT '[]',
            summary_json TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS telemetry_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES telemetry_runs(id) ON DELETE CASCADE,
            ts REAL NOT NULL,
            rpm REAL,
            speed_kmh REAL,
            coolant_c REAL,
            load_pct REAL,
            throttle_pct REAL,
            maf_gps REAL,
            map_kpa REAL,
            stft_b1 REAL,
            ltft_b1 REAL,
            stft_b2 REAL,
            ltft_b2 REAL,
            timing_deg REAL,
            iat_c REAL,
            battery_v REAL,
            o2_b1s1_v REAL,
            o2_b1s2_v REAL,
            knock_deg REAL,
            extra_pids_json TEXT DEFAULT '{}',
            dtcs_json TEXT DEFAULT '[]',
            can_frames_json TEXT DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_tsamp_run ON telemetry_samples(run_id, ts);

        CREATE TABLE IF NOT EXISTS vehicle_pid_cache (
            asset_key TEXT PRIMARY KEY,
            supported_pids_json TEXT NOT NULL DEFAULT '[]',
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vin_cache (
            vin TEXT PRIMARY KEY,
            decoded_json TEXT NOT NULL DEFAULT '{}',
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS custom_pids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER REFERENCES assets(id) ON DELETE CASCADE,
            source_format TEXT NOT NULL DEFAULT 'torque_csv',
            name TEXT NOT NULL,
            short_name TEXT NOT NULL,
            header TEXT DEFAULT '7DF',
            cmd TEXT NOT NULL,
            equation TEXT NOT NULL DEFAULT 'A',
            unit TEXT DEFAULT '',
            min_val REAL DEFAULT 0,
            max_val REAL DEFAULT 100,
            enabled INTEGER DEFAULT 1,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dbc_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER REFERENCES assets(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            source TEXT DEFAULT 'opendbc',
            dbc_text TEXT NOT NULL,
            message_count INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gauge_prefs (
            asset_key TEXT PRIMARY KEY,
            selected_keys_json TEXT NOT NULL DEFAULT '[]',
            updated_at INTEGER NOT NULL
        );
        """
    )
    # Safe additive migration for existing SQLite databases created prior to v2.6.4
    cols = {r[1] for r in conn.execute("PRAGMA table_info(telemetry_samples)").fetchall()}
    if "extra_pids_json" not in cols:
        conn.execute("ALTER TABLE telemetry_samples ADD COLUMN extra_pids_json TEXT DEFAULT '{}'")
    conn.commit()


def _detect_magic_kind(raw: bytes, ext: str) -> Optional[str]:
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if ext in (".csv", ".log", ".txt", ".dbc", ".json"):
        try:
            raw[:8192].decode("utf-8")
            return ext.lstrip(".")
        except UnicodeDecodeError:
            return None
    return None


def save_upload(filename: str, raw: bytes, allowed_exts: tuple[str, ...] = ("jpg", "png", "webp", "csv", "log", "txt", "dbc", "json")) -> Dict[str, Any]:
    """Validate magic bytes & size, generate unpredictable filename, store strictly inside uploads dir."""
    if not raw:
        raise ValueError("Upload payload is empty")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")

    base = os.path.basename(str(filename or "upload.bin"))
    _, ext = os.path.splitext(base.lower())
    detected = _detect_magic_kind(raw, ext)
    if not detected or detected not in allowed_exts:
        raise ValueError(f"Unsupported or invalid file content (allowed: {', '.join(allowed_exts)})")

    up_dir = upload_dir().resolve()
    safe_name = f"{uuid.uuid4().hex}.{detected}"
    target = (up_dir / safe_name).resolve()
    if not str(target).startswith(str(up_dir) + os.sep):
        raise ValueError("Resolved upload path escaped upload boundary")

    with open(target, "wb") as f:
        f.write(raw)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass

    return {
        "status": "ok",
        "file_path": str(target),
        "stored_name": safe_name,
        "original_name": base[:120],
        "kind": detected,
        "size_bytes": len(raw),
    }


def _row_dict(r: sqlite3.Row) -> Dict[str, Any]:
    return dict(r)


def list_assets(conn: sqlite3.Connection, category: Optional[str] = None) -> List[Dict[str, Any]]:
    if category and category != "all":
        rows = conn.execute(
            "SELECT * FROM assets WHERE category = ? ORDER BY updated_at DESC, id DESC",
            (category,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM assets ORDER BY updated_at DESC, id DESC").fetchall()
    return [_row_dict(r) for r in rows]


def save_asset(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    name = str(payload.get("name") or "").strip()[:160]
    if not name:
        raise ValueError("Asset name is required")
    cat = str(payload.get("category") or "vehicle").strip().lower()
    if cat not in ASSET_CATEGORIES:
        cat = "other"
    ts = now_ts()
    aid = payload.get("id")
    if aid:
        conn.execute(
            """UPDATE assets SET name=?, category=?, make=?, model=?, year=?, vin_or_serial=?,
               meter_value=?, meter_unit=?, engine_spec=?, fluid_summary=?, photo_path=?, notes=?, updated_at=?
               WHERE id=?""",
            (
                name,
                cat,
                str(payload.get("make") or "")[:80],
                str(payload.get("model") or "")[:80],
                str(payload.get("year") or "")[:16],
                str(payload.get("vin_or_serial") or "")[:64],
                float(payload.get("meter_value") or 0),
                str(payload.get("meter_unit") or "mi")[:16],
                str(payload.get("engine_spec") or "")[:200],
                str(payload.get("fluid_summary") or "")[:240],
                str(payload.get("photo_path") or "")[:400],
                str(payload.get("notes") or "")[:2000],
                ts,
                int(aid),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (int(aid),)).fetchone()
        return _row_dict(row) if row else {"id": int(aid)}

    cur = conn.execute(
        """INSERT INTO assets (name, category, make, model, year, vin_or_serial, meter_value, meter_unit,
           engine_spec, fluid_summary, photo_path, notes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            cat,
            str(payload.get("make") or "")[:80],
            str(payload.get("model") or "")[:80],
            str(payload.get("year") or "")[:16],
            str(payload.get("vin_or_serial") or "")[:64],
            float(payload.get("meter_value") or 0),
            str(payload.get("meter_unit") or "mi")[:16],
            str(payload.get("engine_spec") or "")[:200],
            str(payload.get("fluid_summary") or "")[:240],
            str(payload.get("photo_path") or "")[:400],
            str(payload.get("notes") or "")[:2000],
            ts,
            ts,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_dict(row)


def delete_asset(conn: sqlite3.Connection, asset_id: int) -> bool:
    cur = conn.execute("DELETE FROM assets WHERE id = ?", (int(asset_id),))
    conn.commit()
    return cur.rowcount > 0


def save_interval(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    asset_id = int(payload["asset_id"])
    task_name = str(payload.get("task_name") or "").strip()[:160]
    if not task_name:
        raise ValueError("task_name is required")
    ts = now_ts()
    iid = payload.get("id")
    if iid:
        conn.execute(
            """UPDATE service_intervals SET task_name=?, interval_value=?, interval_unit=?,
               last_done_meter=?, last_done_date=?, fluid_or_part=?, capacity_spec=?, notes=?, updated_at=?
               WHERE id=? AND asset_id=?""",
            (
                task_name,
                float(payload.get("interval_value") or 0),
                str(payload.get("interval_unit") or "mi")[:16],
                float(payload.get("last_done_meter") or 0),
                str(payload.get("last_done_date") or "")[:32],
                str(payload.get("fluid_or_part") or "")[:160],
                str(payload.get("capacity_spec") or "")[:80],
                str(payload.get("notes") or "")[:500],
                ts,
                int(iid),
                asset_id,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM service_intervals WHERE id = ?", (int(iid),)).fetchone()
        return _row_dict(row)

    cur = conn.execute(
        """INSERT INTO service_intervals (asset_id, task_name, interval_value, interval_unit,
           last_done_meter, last_done_date, fluid_or_part, capacity_spec, notes, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            asset_id,
            task_name,
            float(payload.get("interval_value") or 0),
            str(payload.get("interval_unit") or "mi")[:16],
            float(payload.get("last_done_meter") or 0),
            str(payload.get("last_done_date") or "")[:32],
            str(payload.get("fluid_or_part") or "")[:160],
            str(payload.get("capacity_spec") or "")[:80],
            str(payload.get("notes") or "")[:500],
            ts,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM service_intervals WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_dict(row)


def save_torque_spec(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    asset_id = int(payload["asset_id"])
    comp = str(payload.get("component") or "").strip()[:160]
    val = str(payload.get("spec_value") or "").strip()[:48]
    if not comp or not val:
        raise ValueError("component and spec_value are required")
    cur = conn.execute(
        """INSERT INTO torque_specs (asset_id, component, spec_value, unit, fastener_size, sequence_notes, source, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            asset_id,
            comp,
            val,
            str(payload.get("unit") or "ft-lb")[:16],
            str(payload.get("fastener_size") or "")[:40],
            str(payload.get("sequence_notes") or "")[:300],
            str(payload.get("source") or "manual")[:80],
            now_ts(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM torque_specs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_dict(row)


def save_part(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    name = str(payload.get("part_name") or "").strip()[:160]
    if not name:
        raise ValueError("part_name is required")
    asset_id = int(payload["asset_id"]) if payload.get("asset_id") else None
    ts = now_ts()
    pid = payload.get("id")
    if pid:
        conn.execute(
            """UPDATE parts_inventory SET asset_id=?, part_name=?, oem_number=?, aftermarket_xref=?,
               category=?, qty_on_hand=?, min_qty=?, unit_cost=?, location_bin=?, notes=?, updated_at=?
               WHERE id=?""",
            (
                asset_id,
                name,
                str(payload.get("oem_number") or "")[:80],
                str(payload.get("aftermarket_xref") or "")[:240],
                str(payload.get("category") or "consumable")[:48],
                int(payload.get("qty_on_hand") or 0),
                int(payload.get("min_qty") or 1),
                float(payload.get("unit_cost") or 0),
                str(payload.get("location_bin") or "")[:64],
                str(payload.get("notes") or "")[:500],
                ts,
                int(pid),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM parts_inventory WHERE id = ?", (int(pid),)).fetchone()
        return _row_dict(row)

    cur = conn.execute(
        """INSERT INTO parts_inventory (asset_id, part_name, oem_number, aftermarket_xref, category,
           qty_on_hand, min_qty, unit_cost, location_bin, notes, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            asset_id,
            name,
            str(payload.get("oem_number") or "")[:80],
            str(payload.get("aftermarket_xref") or "")[:240],
            str(payload.get("category") or "consumable")[:48],
            int(payload.get("qty_on_hand") or 0),
            int(payload.get("min_qty") or 1),
            float(payload.get("unit_cost") or 0),
            str(payload.get("location_bin") or "")[:64],
            str(payload.get("notes") or "")[:500],
            ts,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM parts_inventory WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_dict(row)


def save_maintenance_log(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    asset_id = int(payload["asset_id"])
    title = str(payload.get("title") or "").strip()[:180]
    if not title:
        raise ValueError("title is required")
    checklist = payload.get("checklist")
    if isinstance(checklist, list):
        checklist_json = json.dumps(checklist)
    elif isinstance(payload.get("checklist_json"), str):
        checklist_json = payload["checklist_json"]
    else:
        checklist_json = "[]"
    cur = conn.execute(
        """INSERT INTO maintenance_logs (asset_id, log_type, title, meter_value, summary, checklist_json, photo_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            asset_id,
            str(payload.get("log_type") or "service")[:32],
            title,
            float(payload.get("meter_value") or 0),
            str(payload.get("summary") or "")[:8000],
            checklist_json,
            str(payload.get("photo_path") or "")[:400],
            now_ts(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM maintenance_logs WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_dict(row)


def save_obd_session(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    asset_id = int(payload["asset_id"]) if payload.get("asset_id") else None
    cur = conn.execute(
        """INSERT INTO obd_sessions (asset_id, adapter_type, port_or_addr, vin, protocol, battery_v,
           pids_json, dtcs_json, freeze_frame_json, readiness_json, raw_notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            asset_id,
            str(payload.get("adapter_type") or "sim")[:40],
            str(payload.get("port_or_addr") or "")[:120],
            str(payload.get("vin") or "")[:32],
            str(payload.get("protocol") or "")[:80],
            float(payload.get("battery_v") or 0),
            json.dumps(payload.get("pids") or {}),
            json.dumps(payload.get("dtcs") or []),
            json.dumps(payload.get("freeze_frame") or {}),
            json.dumps(payload.get("readiness") or {}),
            str(payload.get("raw_notes") or "")[:2000],
            now_ts(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM obd_sessions WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_dict(row)


def save_can_capture(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    asset_id = int(payload["asset_id"]) if payload.get("asset_id") else None
    cur = conn.execute(
        """INSERT INTO can_captures (asset_id, interface, frame_count, unique_ids_json, decoded_signals_json, summary, raw_sample, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            asset_id,
            str(payload.get("interface") or "candump")[:40],
            int(payload.get("frame_count") or 0),
            json.dumps(payload.get("unique_ids") or []),
            json.dumps(payload.get("decoded_signals") or []),
            str(payload.get("summary") or "")[:2000],
            str(payload.get("raw_sample") or "")[:6000],
            now_ts(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM can_captures WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_dict(row)


def create_telemetry_run(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    asset_id = int(payload["asset_id"]) if payload.get("asset_id") else None
    v_year = str(payload.get("vehicle_year") or "")[:16]
    v_make = str(payload.get("vehicle_make") or "")[:80]
    v_model = str(payload.get("vehicle_model") or "")[:80]
    vin = str(payload.get("vin") or "")[:32]
    if asset_id and not (v_year or v_make or v_model):
        arow = conn.execute("SELECT year, make, model, vin_or_serial FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if arow:
            v_year = v_year or str(arow["year"] or "")
            v_make = v_make or str(arow["make"] or "")
            v_model = v_model or str(arow["model"] or "")
            vin = vin or str(arow["vin_or_serial"] or "")
    label = str(payload.get("run_label") or f"{v_year} {v_make} {v_model} Telemetry".strip() or "Live Telemetry Run")[:160]
    ts_f = time.time()
    cur = conn.execute(
        """INSERT INTO telemetry_runs (asset_id, run_label, transport, port_or_addr, vehicle_year, vehicle_make,
           vehicle_model, vin, sim_profile, started_at, ended_at, sample_count, active_dtcs_json, summary_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '[]', '{}')""",
        (
            asset_id,
            label,
            str(payload.get("transport") or "sim")[:32],
            str(payload.get("port_or_addr") or "")[:120],
            v_year,
            v_make,
            v_model,
            vin,
            str(payload.get("sim_profile") or "")[:64],
            ts_f,
        ),
    )
    conn.commit()
    return _row_dict(conn.execute("SELECT * FROM telemetry_runs WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_latest_telemetry_sample(conn: sqlite3.Connection, run_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM telemetry_samples WHERE run_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
        (int(run_id),),
    ).fetchone()
    if not row:
        return None
    sd = _row_dict(row)
    try:
        sd["extra_pids"] = json.loads(sd.get("extra_pids_json") or "{}")
    except Exception:
        sd["extra_pids"] = {}
    return sd


def append_telemetry_sample(conn: sqlite3.Connection, run_id: int, sample: Dict[str, Any], rolling_cap: int = 600) -> int:
    ts_f = float(sample.get("ts") or time.time())
    dtcs = sample.get("dtcs") or []
    can_frames = sample.get("can_frames") or []
    extra_pids = sample.get("extra_pids") if isinstance(sample.get("extra_pids"), dict) else {}

    # Carry-forward slow/rotating rich extra PIDs from previous sample so every buffered row is a complete Copilot snapshot
    prev = get_latest_telemetry_sample(conn, int(run_id))
    merged_extra: Dict[str, Any] = {}
    if prev and isinstance(prev.get("extra_pids"), dict):
        merged_extra.update(prev["extra_pids"])
    for ek, ev in extra_pids.items():
        if ev is not None:
            merged_extra[ek] = ev

    cur = conn.execute(
        """INSERT INTO telemetry_samples (run_id, ts, rpm, speed_kmh, coolant_c, load_pct, throttle_pct,
           maf_gps, map_kpa, stft_b1, ltft_b1, stft_b2, ltft_b2, timing_deg, iat_c, battery_v,
           o2_b1s1_v, o2_b1s2_v, knock_deg, extra_pids_json, dtcs_json, can_frames_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(run_id),
            ts_f,
            sample.get("rpm"),
            sample.get("speed_kmh"),
            sample.get("coolant_c"),
            sample.get("load_pct"),
            sample.get("throttle_pct"),
            sample.get("maf_gps"),
            sample.get("map_kpa"),
            sample.get("stft_b1"),
            sample.get("ltft_b1"),
            sample.get("stft_b2"),
            sample.get("ltft_b2"),
            sample.get("timing_deg"),
            sample.get("iat_c"),
            sample.get("battery_v"),
            sample.get("o2_b1s1_v"),
            sample.get("o2_b1s2_v"),
            sample.get("knock_deg"),
            json.dumps(merged_extra),
            json.dumps(dtcs),
            json.dumps(can_frames),
        ),
    )
    # Enforce rolling ring-buffer cap (default 600 latest samples per run) so SQLite stays lean & fast
    if rolling_cap and rolling_cap > 0:
        conn.execute(
            """DELETE FROM telemetry_samples
               WHERE run_id = ? AND id NOT IN (
                   SELECT id FROM telemetry_samples WHERE run_id = ? ORDER BY ts DESC, id DESC LIMIT ?
               )""",
            (int(run_id), int(run_id), int(rolling_cap)),
        )
    buf_cnt = conn.execute("SELECT COUNT(*) FROM telemetry_samples WHERE run_id = ?", (int(run_id),)).fetchone()[0]
    if sample.get("dtcs_checked", True) or dtcs:
        codes = sorted({(d.get("code") if isinstance(d, dict) else str(d)) for d in dtcs if d})
        conn.execute(
            "UPDATE telemetry_runs SET sample_count = ?, ended_at = ?, active_dtcs_json = ? WHERE id = ?",
            (int(buf_cnt), ts_f, json.dumps(codes), int(run_id)),
        )
    else:
        conn.execute(
            "UPDATE telemetry_runs SET sample_count = ?, ended_at = ? WHERE id = ?",
            (int(buf_cnt), ts_f, int(run_id)),
        )
    conn.commit()
    sample["extra_pids"] = merged_extra
    for ek, ev in merged_extra.items():
        if ek not in sample:
            sample[ek] = ev
    sample["buffered_samples"] = int(buf_cnt)
    return int(cur.lastrowid)


def save_supported_pids(conn: sqlite3.Connection, pids: List[str], asset_id: Optional[int] = None, port_or_addr: str = "") -> List[str]:
    clean = sorted({str(p).upper().zfill(2) for p in pids if re.match(r"^[0-9A-Fa-f]{1,2}$", str(p).strip())})
    if not clean:
        return []
    key = f"asset_{asset_id}" if asset_id else (f"port_{re.sub(r'[^A-Za-z0-9_.:-]', '', port_or_addr)[:48]}" if port_or_addr else "default")
    conn.execute(
        "INSERT INTO vehicle_pid_cache (asset_key, supported_pids_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(asset_key) DO UPDATE SET supported_pids_json=excluded.supported_pids_json, updated_at=excluded.updated_at",
        (key, json.dumps(clean), now_ts()),
    )
    conn.commit()
    return clean


def get_supported_pids(conn: sqlite3.Connection, asset_id: Optional[int] = None, port_or_addr: str = "") -> List[str]:
    keys = []
    if asset_id:
        keys.append(f"asset_{asset_id}")
    if port_or_addr:
        keys.append(f"port_{re.sub(r'[^A-Za-z0-9_.:-]', '', port_or_addr)[:48]}")
    keys.append("default")
    for k in keys:
        r = conn.execute("SELECT supported_pids_json FROM vehicle_pid_cache WHERE asset_key = ?", (k,)).fetchone()
        if r:
            try:
                arr = json.loads(r["supported_pids_json"] or "[]")
                if isinstance(arr, list) and arr:
                    return [str(x).upper().zfill(2) for x in arr]
            except Exception:
                pass
    return []


def get_or_decode_vin(conn: sqlite3.Connection, vin: str, allow_network: bool = True) -> Dict[str, Any]:
    """Return cached NHTSA vPIC VIN decode or decode & cache it in SQLite."""
    import vin_decoder
    v = vin_decoder.normalize_vin(vin)
    if not vin_decoder.VIN_RE.match(v):
        return {"status": "error", "vin": v, "message": f"Invalid VIN: {vin}"}
    row = conn.execute("SELECT decoded_json FROM vin_cache WHERE vin = ?", (v,)).fetchone()
    if row:
        try:
            cached = json.loads(row["decoded_json"] or "{}")
            if cached.get("status") == "ok" and (cached.get("source") == "nhtsa_vpic" or not allow_network):
                return cached
        except Exception:
            pass
    dec = vin_decoder.decode_vin(v, allow_network=allow_network)
    if dec.get("status") == "ok":
        conn.execute(
            "INSERT INTO vin_cache (vin, decoded_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(vin) DO UPDATE SET decoded_json=excluded.decoded_json, updated_at=excluded.updated_at",
            (v, json.dumps(dec), now_ts()),
        )
        conn.commit()
    return dec


def update_telemetry_run_vehicle(
    conn: sqlite3.Connection,
    run_id: int,
    vin: str = "",
    year: str = "",
    make: str = "",
    model: str = "",
) -> None:
    label_parts = [p for p in (year, make, model) if p]
    if label_parts:
        lbl = f"{' '.join(label_parts)} Telemetry"[:160]
        conn.execute(
            "UPDATE telemetry_runs SET vin = CASE WHEN ? != '' THEN ? ELSE vin END, vehicle_year = CASE WHEN ? != '' THEN ? ELSE vehicle_year END, vehicle_make = CASE WHEN ? != '' THEN ? ELSE vehicle_make END, vehicle_model = CASE WHEN ? != '' THEN ? ELSE vehicle_model END, run_label = ? WHERE id = ?",
            (vin[:32], vin[:32], year[:16], year[:16], make[:80], make[:80], model[:80], model[:80], lbl, int(run_id)),
        )
    elif vin:
        conn.execute("UPDATE telemetry_runs SET vin = ? WHERE id = ?", (vin[:32], int(run_id)))
    conn.commit()


def get_telemetry_samples_after(conn: sqlite3.Connection, run_id: int, after_id: int = 0, limit: int = 120) -> List[Dict[str, Any]]:
    """Drain newly buffered samples since after_id (chronological order) for smooth UI buffer-and-drain."""
    rows = conn.execute(
        "SELECT * FROM telemetry_samples WHERE run_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (int(run_id), int(after_id), int(limit)),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for x in rows:
        sd = _row_dict(x)
        try:
            sd["dtcs"] = json.loads(sd.get("dtcs_json") or "[]")
        except Exception:
            sd["dtcs"] = []
        try:
            ep = json.loads(sd.get("extra_pids_json") or "{}")
            sd["extra_pids"] = ep if isinstance(ep, dict) else {}
        except Exception:
            sd["extra_pids"] = {}
        for ek, ev in sd["extra_pids"].items():
            if ek not in sd:
                sd[ek] = ev
        out.append(sd)
    return out


def query_telemetry_slice(
    conn: sqlite3.Connection,
    run_id: int,
    sort_by: str = "ts",
    desc: bool = False,
    filter_channel: str = "",
    min_val: Optional[float] = None,
    max_val: Optional[float] = None,
    limit: int = 15,
) -> Dict[str, Any]:
    """Flexible raw-row inspection over the rolling 600-sample buffer so Copilot can verify hypotheses against real samples."""
    run, samples = get_telemetry_run_samples(conn, int(run_id), limit=600)
    if not run:
        return {"status": "error", "message": f"Telemetry run {run_id} not found"}
    if not samples:
        return {"status": "empty", "run_id": int(run_id), "rows": [], "total_buffered": 0}
    t0 = float(samples[0].get("ts") or 0.0)
    enriched: List[Dict[str, Any]] = []
    for s in samples:
        row: Dict[str, Any] = {"id": s["id"], "rel_sec": round(float(s.get("ts") or t0) - t0, 1)}
        for k in (
            "rpm", "speed_kmh", "coolant_c", "load_pct", "throttle_pct", "maf_gps", "map_kpa",
            "stft_b1", "ltft_b1", "stft_b2", "ltft_b2", "timing_deg", "iat_c", "battery_v",
            "o2_b1s1_v", "o2_b1s2_v",
        ):
            if s.get(k) is not None:
                row[k] = s[k]
        if row.get("stft_b1") is not None and row.get("ltft_b1") is not None:
            row["total_ft_b1"] = round(float(row["stft_b1"]) + float(row["ltft_b1"]), 2)
        for ek, ev in (s.get("extra_pids") or {}).items():
            if ev is not None:
                row[ek] = ev
        if filter_channel:
            fv = row.get(filter_channel)
            if fv is None:
                continue
            try:
                fvf = float(fv)
            except (TypeError, ValueError):
                continue
            if min_val is not None and fvf < float(min_val):
                continue
            if max_val is not None and fvf > float(max_val):
                continue
        enriched.append(row)

    if sort_by and sort_by != "ts":
        enriched.sort(
            key=lambda r: (r.get(sort_by) is None, -(float(r[sort_by])) if (desc and r.get(sort_by) is not None) else (float(r[sort_by]) if r.get(sort_by) is not None else 0.0))
        )
    elif desc:
        enriched.reverse()

    lim = max(1, min(40, int(limit or 15)))
    return {
        "status": "ok",
        "run_id": int(run_id),
        "total_buffered": len(samples),
        "matched_count": len(enriched),
        "returned_count": min(lim, len(enriched)),
        "sort_by": sort_by,
        "desc": bool(desc),
        "filter": {"channel": filter_channel, "min_val": min_val, "max_val": max_val} if filter_channel else None,
        "rows": enriched[:lim],
    }


def list_telemetry_runs(conn: sqlite3.Connection, asset_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
    if asset_id is not None:
        rows = conn.execute(
            "SELECT * FROM telemetry_runs WHERE asset_id = ? OR asset_id IS NULL ORDER BY started_at DESC, id DESC LIMIT ?",
            (int(asset_id), int(limit)),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM telemetry_runs ORDER BY started_at DESC, id DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = _row_dict(r)
        try:
            d["active_dtcs"] = json.loads(d.get("active_dtcs_json") or "[]")
        except Exception:
            d["active_dtcs"] = []
        out.append(d)
    return out


def get_telemetry_run_samples(conn: sqlite3.Connection, run_id: int, limit: int = 600) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    rrow = conn.execute("SELECT * FROM telemetry_runs WHERE id = ?", (int(run_id),)).fetchone()
    if not rrow:
        return None, []
    run = _row_dict(rrow)
    try:
        run["active_dtcs"] = json.loads(run.get("active_dtcs_json") or "[]")
    except Exception:
        run["active_dtcs"] = []
    rows = conn.execute(
        """SELECT * FROM (
               SELECT * FROM telemetry_samples WHERE run_id = ? ORDER BY ts DESC, id DESC LIMIT ?
           ) ORDER BY ts ASC, id ASC""",
        (int(run_id), int(limit)),
    ).fetchall()
    samples = []
    for x in rows:
        sd = _row_dict(x)
        try:
            sd["dtcs"] = json.loads(sd.get("dtcs_json") or "[]")
        except Exception:
            sd["dtcs"] = []
        try:
            sd["can_frames"] = json.loads(sd.get("can_frames_json") or "[]")
        except Exception:
            sd["can_frames"] = []
        try:
            ep = json.loads(sd.get("extra_pids_json") or "{}")
            sd["extra_pids"] = ep if isinstance(ep, dict) else {}
        except Exception:
            sd["extra_pids"] = {}
        for ek, ev in sd["extra_pids"].items():
            if ek not in sd:
                sd[ek] = ev
        samples.append(sd)
    return run, samples


def collect_historical_dtcs(conn: sqlite3.Connection, asset_id: Optional[int] = None, include_sim: bool = False) -> List[str]:
    """Gather unique historical DTC codes across past OBD sessions and recorded telemetry runs.
    Excludes simulator ('sim') runs/sessions by default so bench test codes NEVER poison real vehicle triage!"""
    codes = set()
    sim_s_clause = "" if include_sim else " AND adapter_type != 'sim'"
    sim_r_clause = "" if include_sim else " AND transport != 'sim'"
    if asset_id is not None:
        srows = conn.execute(f"SELECT dtcs_json FROM obd_sessions WHERE asset_id = ?{sim_s_clause}", (int(asset_id),)).fetchall()
        rrows = conn.execute(f"SELECT active_dtcs_json FROM telemetry_runs WHERE asset_id = ?{sim_r_clause}", (int(asset_id),)).fetchall()
    else:
        srows = conn.execute(f"SELECT dtcs_json FROM obd_sessions WHERE 1=1{sim_s_clause} ORDER BY created_at DESC LIMIT 20").fetchall()
        rrows = conn.execute(f"SELECT active_dtcs_json FROM telemetry_runs WHERE 1=1{sim_r_clause} ORDER BY started_at DESC LIMIT 20").fetchall()
    for r in srows:
        try:
            for d in json.loads(r["dtcs_json"] or "[]"):
                c = d.get("code") if isinstance(d, dict) else str(d)
                if c:
                    codes.add(c)
        except Exception:
            pass
    for r in rrows:
        try:
            for c in json.loads(r["active_dtcs_json"] or "[]"):
                if c:
                    codes.add(str(c))
        except Exception:
            pass
    return sorted(codes)


def clear_telemetry_data(conn: sqlite3.Connection, scope: str = "sim", asset_id: Optional[int] = None) -> Dict[str, Any]:
    """Clear simulator test runs/codes ('sim'), current vehicle telemetry buffer ('buffer'), or all runs/captures ('all_runs').
    Preserves saved vehicle assets, service intervals, torque specs, parts, maintenance logs, and DBC profiles."""
    sc = str(scope or "sim").strip().lower()
    deleted_runs = 0
    deleted_sessions = 0
    deleted_cans = 0
    cleared_pid_cache = 0

    if sc == "sim":
        deleted_runs = conn.execute("DELETE FROM telemetry_runs WHERE transport = 'sim'").rowcount
        deleted_sessions = conn.execute("DELETE FROM obd_sessions WHERE adapter_type = 'sim'").rowcount
        deleted_cans = conn.execute("DELETE FROM can_captures WHERE interface LIKE 'sim%'").rowcount
    elif sc == "buffer":
        if asset_id is not None:
            deleted_runs = conn.execute("DELETE FROM telemetry_runs WHERE asset_id = ? OR asset_id IS NULL", (int(asset_id),)).rowcount
            deleted_sessions = conn.execute("DELETE FROM obd_sessions WHERE adapter_type = 'sim' AND (asset_id = ? OR asset_id IS NULL)", (int(asset_id),)).rowcount
            deleted_cans = conn.execute("DELETE FROM can_captures WHERE asset_id = ? OR asset_id IS NULL", (int(asset_id),)).rowcount
            cleared_pid_cache = conn.execute("DELETE FROM vehicle_pid_cache WHERE asset_key = ? OR asset_key LIKE 'port_%'", (f"asset_{int(asset_id)}",)).rowcount
        else:
            deleted_runs = conn.execute("DELETE FROM telemetry_runs WHERE asset_id IS NULL OR transport = 'sim'").rowcount
            deleted_sessions = conn.execute("DELETE FROM obd_sessions WHERE asset_id IS NULL OR adapter_type = 'sim'").rowcount
            deleted_cans = conn.execute("DELETE FROM can_captures WHERE asset_id IS NULL OR interface LIKE 'sim%'").rowcount
            cleared_pid_cache = conn.execute("DELETE FROM vehicle_pid_cache").rowcount
    elif sc == "all_runs":
        deleted_runs = conn.execute("DELETE FROM telemetry_runs").rowcount
        deleted_sessions = conn.execute("DELETE FROM obd_sessions").rowcount
        deleted_cans = conn.execute("DELETE FROM can_captures").rowcount
        cleared_pid_cache = conn.execute("DELETE FROM vehicle_pid_cache").rowcount
    else:
        raise ValueError(f"Unsupported clear scope '{scope}' (allowed: sim, buffer, all_runs)")

    conn.commit()
    return {
        "status": "ok",
        "scope": sc,
        "deleted_runs": int(deleted_runs or 0),
        "deleted_sessions": int(deleted_sessions or 0),
        "deleted_can_captures": int(deleted_cans or 0),
        "cleared_pid_cache": int(cleared_pid_cache or 0),
    }


def save_custom_pids(conn: sqlite3.Connection, pids: List[Dict[str, Any]], asset_id: Optional[int] = None) -> int:
    ts = now_ts()
    aid = int(asset_id) if asset_id else None
    added = 0
    for p in pids:
        conn.execute(
            """INSERT INTO custom_pids (asset_id, source_format, name, short_name, header, cmd, equation, unit, min_val, max_val, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                aid,
                str(p.get("source_format") or "torque_csv")[:32],
                str(p["name"])[:120],
                str(p.get("short_name") or p["name"])[:24],
                str(p.get("header") or "7DF")[:8],
                str(p["cmd"])[:12],
                str(p.get("equation") or "A")[:160],
                str(p.get("unit") or "")[:24],
                float(p.get("min_val") or 0),
                float(p.get("max_val") or 100),
                ts,
            ),
        )
        added += 1
    conn.commit()
    return added


def list_custom_pids(conn: sqlite3.Connection, asset_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if asset_id is not None:
        rows = conn.execute(
            "SELECT * FROM custom_pids WHERE asset_id = ? OR asset_id IS NULL ORDER BY id DESC LIMIT 200",
            (int(asset_id),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM custom_pids ORDER BY id DESC LIMIT 200").fetchall()
    return [_row_dict(r) for r in rows]


def delete_custom_pid(conn: sqlite3.Connection, pid_id: int) -> bool:
    cur = conn.execute("DELETE FROM custom_pids WHERE id = ?", (int(pid_id),))
    conn.commit()
    return cur.rowcount > 0


def save_dbc_profile(conn: sqlite3.Connection, name: str, dbc_text: str, message_count: int, asset_id: Optional[int] = None, source: str = "opendbc") -> Dict[str, Any]:
    aid = int(asset_id) if asset_id else None
    cur = conn.execute(
        "INSERT INTO dbc_profiles (asset_id, name, source, dbc_text, message_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (aid, str(name)[:120], str(source)[:40], dbc_text[:500000], int(message_count), now_ts()),
    )
    conn.commit()
    return _row_dict(conn.execute("SELECT id, asset_id, name, source, message_count, created_at FROM dbc_profiles WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_dbc_profiles(conn: sqlite3.Connection, asset_id: Optional[int] = None) -> List[Dict[str, Any]]:
    if asset_id is not None:
        rows = conn.execute(
            "SELECT id, asset_id, name, source, dbc_text, message_count, created_at FROM dbc_profiles WHERE asset_id = ? OR asset_id IS NULL ORDER BY id DESC LIMIT 30",
            (int(asset_id),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT id, asset_id, name, source, dbc_text, message_count, created_at FROM dbc_profiles ORDER BY id DESC LIMIT 30").fetchall()
    return [_row_dict(r) for r in rows]


def delete_dbc_profile(conn: sqlite3.Connection, profile_id: int) -> bool:
    cur = conn.execute("DELETE FROM dbc_profiles WHERE id = ?", (int(profile_id),))
    conn.commit()
    return cur.rowcount > 0


def get_active_dbc_text(conn: sqlite3.Connection, asset_id: Optional[int] = None, dbc_id: Optional[int] = None) -> str:
    if dbc_id:
        r = conn.execute("SELECT dbc_text FROM dbc_profiles WHERE id = ?", (int(dbc_id),)).fetchone()
        return str(r["dbc_text"]) if r else ""
    if asset_id is not None:
        r = conn.execute("SELECT dbc_text FROM dbc_profiles WHERE asset_id = ? OR asset_id IS NULL ORDER BY id DESC LIMIT 1", (int(asset_id),)).fetchone()
        return str(r["dbc_text"]) if r else ""
    r = conn.execute("SELECT dbc_text FROM dbc_profiles ORDER BY id DESC LIMIT 1").fetchone()
    return str(r["dbc_text"]) if r else ""


def get_gauge_prefs(conn: sqlite3.Connection, asset_id: Optional[int] = None) -> List[str]:
    key = f"asset_{asset_id}" if asset_id else "default"
    r = conn.execute("SELECT selected_keys_json FROM gauge_prefs WHERE asset_key = ?", (key,)).fetchone()
    if r:
        try:
            arr = json.loads(r["selected_keys_json"])
            if isinstance(arr, list) and arr:
                return [str(x) for x in arr[:12]]
        except Exception:
            pass
    return ["rpm", "battery_v", "coolant_c", "total_ft_b1", "load_pct", "o2_b1s1_v"]


def save_gauge_prefs(conn: sqlite3.Connection, keys: List[str], asset_id: Optional[int] = None) -> List[str]:
    key = f"asset_{asset_id}" if asset_id else "default"
    clean = [re.sub(r"[^A-Za-z0-9_:-]", "", str(k))[:48] for k in keys if str(k).strip()][:12]
    conn.execute(
        "INSERT INTO gauge_prefs (asset_key, selected_keys_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(asset_key) DO UPDATE SET selected_keys_json=excluded.selected_keys_json, updated_at=excluded.updated_at",
        (key, json.dumps(clean), now_ts()),
    )
    conn.commit()
    return clean


def get_asset_bundle(conn: sqlite3.Connection, asset_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    if asset_id is None:
        r = conn.execute("SELECT * FROM assets ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
    else:
        r = conn.execute("SELECT * FROM assets WHERE id = ?", (int(asset_id),)).fetchone()
    if not r:
        return None
    asset = _row_dict(r)
    aid = asset["id"]
    meter = float(asset.get("meter_value") or 0)

    intervals = []
    for row in conn.execute("SELECT * FROM service_intervals WHERE asset_id = ? ORDER BY id ASC", (aid,)).fetchall():
        d = _row_dict(row)
        iv = float(d.get("interval_value") or 0)
        last_m = float(d.get("last_done_meter") or 0)
        if iv > 0 and d.get("interval_unit") in ("mi", "km", "hrs"):
            next_due = last_m + iv
            remaining = round(next_due - meter, 1)
            d["next_due_meter"] = round(next_due, 1)
            d["remaining"] = remaining
            d["due_state"] = "OVERDUE" if remaining <= 0 else ("DUE_SOON" if remaining <= iv * 0.15 else "OK")
        else:
            d["due_state"] = "TRACKED"
        intervals.append(d)

    torques = [_row_dict(x) for x in conn.execute("SELECT * FROM torque_specs WHERE asset_id = ? ORDER BY component ASC", (aid,)).fetchall()]
    parts = [_row_dict(x) for x in conn.execute("SELECT * FROM parts_inventory WHERE asset_id = ? OR asset_id IS NULL ORDER BY part_name ASC", (aid,)).fetchall()]
    logs = []
    for x in conn.execute("SELECT * FROM maintenance_logs WHERE asset_id = ? ORDER BY created_at DESC, id DESC LIMIT 30", (aid,)).fetchall():
        ld = _row_dict(x)
        try:
            ld["checklist"] = json.loads(ld.get("checklist_json") or "[]")
        except Exception:
            ld["checklist"] = []
        logs.append(ld)

    obd = []
    for x in conn.execute("SELECT * FROM obd_sessions WHERE asset_id = ? OR asset_id IS NULL ORDER BY created_at DESC, id DESC LIMIT 15", (aid,)).fetchall():
        od = _row_dict(x)
        for k in ("pids", "dtcs", "freeze_frame", "readiness"):
            try:
                od[k] = json.loads(od.get(f"{k}_json") or ("[]" if k == "dtcs" else "{}"))
            except Exception:
                od[k] = [] if k == "dtcs" else {}
        obd.append(od)

    cans = []
    for x in conn.execute("SELECT * FROM can_captures WHERE asset_id = ? OR asset_id IS NULL ORDER BY created_at DESC, id DESC LIMIT 15", (aid,)).fetchall():
        cd = _row_dict(x)
        for k in ("unique_ids", "decoded_signals"):
            try:
                cd[k] = json.loads(cd.get(f"{k}_json") or "[]")
            except Exception:
                cd[k] = []
        cans.append(cd)

    return {
        "asset": asset,
        "intervals": intervals,
        "torque_specs": torques,
        "parts": parts,
        "logs": logs,
        "obd_sessions": obd,
        "can_captures": cans,
        "telemetry_runs": list_telemetry_runs(conn, asset_id=aid, limit=15),
        "historical_dtcs": collect_historical_dtcs(conn, asset_id=aid),
        "custom_pids": list_custom_pids(conn, asset_id=aid),
        "dbc_profiles": list_dbc_profiles(conn, asset_id=aid),
        "gauge_prefs": get_gauge_prefs(conn, asset_id=aid),
    }


def seed_demo_shop(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Optional one-click starter bench seed for first-time exploration (idempotent)."""
    existing = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    if existing > 0:
        return {"status": "ok", "seeded": False, "message": "Bench already has assets."}
    truck = save_asset(conn, {
        "name": "Shop Pickup (Sample 5.3L V8)",
        "category": "vehicle",
        "make": "Demo",
        "model": "1500 4WD",
        "year": "2018",
        "vin_or_serial": "1GCVKREC0JZ000001",
        "meter_value": 84250,
        "meter_unit": "mi",
        "engine_spec": "5.3L EcoTec3 V8 (L83) · 0W-20 dexos1",
        "fluid_summary": "Engine oil 8.0 qt 0W-20 · Coolant 16.4 qt Dex-Cool · Diff 75W-90",
        "notes": "Sample vehicle asset. Edit or replace with your real vehicles, small engines, or HVAC units.",
    })
    save_interval(conn, {
        "asset_id": truck["id"],
        "task_name": "Engine Oil & Filter",
        "interval_value": 5000,
        "interval_unit": "mi",
        "last_done_meter": 79800,
        "last_done_date": "2026-08-15",
        "fluid_or_part": "0W-20 Full Synthetic + PF63E Filter",
        "capacity_spec": "8.0 qt",
    })
    save_interval(conn, {
        "asset_id": truck["id"],
        "task_name": "Spark Plugs & Wires Inspection",
        "interval_value": 60000,
        "interval_unit": "mi",
        "last_done_meter": 24000,
        "last_done_date": "2023-05-10",
        "fluid_or_part": "ACDelco 41-114 Iridium",
        "capacity_spec": "Gap 0.040 in",
    })
    save_torque_spec(conn, {
        "asset_id": truck["id"],
        "component": "Wheel Lug Nuts",
        "spec_value": "140",
        "unit": "ft-lb",
        "fastener_size": "M14x1.5",
        "sequence_notes": "6-lug star pattern in 2 passes; re-torque after 50 mi",
        "source": "OEM FSM",
    })
    save_torque_spec(conn, {
        "asset_id": truck["id"],
        "component": "Oil Drain Plug",
        "spec_value": "18",
        "unit": "ft-lb",
        "fastener_size": "M12x1.75",
        "sequence_notes": "Replace crush gasket if weeping",
        "source": "OEM FSM",
    })
    save_part(conn, {
        "asset_id": truck["id"],
        "part_name": "Engine Oil Filter",
        "oem_number": "ACDelco PF63E",
        "aftermarket_xref": "WIX 57045, Mobil1 M1-212A, Fram XG10575",
        "category": "filter",
        "qty_on_hand": 2,
        "min_qty": 1,
        "unit_cost": 7.50,
        "location_bin": "Shelf B2",
    })
    mower = save_asset(conn, {
        "name": "Homestead Zero-Turn Mower (Sample 23HP)",
        "category": "small_engine",
        "make": "Kawasaki",
        "model": "FR691V",
        "year": "2021",
        "vin_or_serial": "FR691V-SAMPLE-01",
        "meter_value": 148.5,
        "meter_unit": "hrs",
        "engine_spec": "726cc V-Twin · 10W-40",
        "fluid_summary": "Oil 2.1 qt 10W-40 · Hydro 20W-50",
    })
    save_interval(conn, {
        "asset_id": mower["id"],
        "task_name": "Oil, Filter & Blade Balance",
        "interval_value": 50,
        "interval_unit": "hrs",
        "last_done_meter": 100,
        "last_done_date": "2026-06-01",
        "fluid_or_part": "10W-40 + 49065-0721 Filter",
        "capacity_spec": "2.1 qt",
    })
    return {"status": "ok", "seeded": True, "assets_created": 2}
