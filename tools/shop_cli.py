#!/usr/bin/env python3
"""
tools/shop_cli.py — Unified JSON CLI for mod-shop-bench agent tools and web actions.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bt_bridge  # noqa: E402
import obd_can  # noqa: E402
import pid_catalog  # noqa: E402
import report_export  # noqa: E402
import shop_db  # noqa: E402
import signal_analytics  # noqa: E402
import telemetry_collector  # noqa: E402
import vin_decoder  # noqa: E402


def _opt_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _opt_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


@contextmanager
def _pause_collector_for_live_op(transport: str) -> Iterator[None]:
    """Briefly pause the persistent background collector so on-demand Copilot scans/queries never contend on the single bridge socket."""
    if str(transport or "sim").strip().lower() not in ("tcp", "serial"):
        yield
        return
    pf = telemetry_collector.pause_file()
    had_alive = False
    try:
        st = telemetry_collector.read_status()
        if st.get("alive") and telemetry_collector.is_pid_alive(int(st.get("pid") or 0)):
            had_alive = True
            pf.write_text("pause", encoding="ascii")
            # Wait up to 2.5s for collector to release its socket cleanly
            deadline = time.time() + 2.5
            while time.time() < deadline:
                cur = telemetry_collector.read_status()
                if cur.get("state") in ("paused", "stopped", "error"):
                    break
                time.sleep(0.1)
        yield
    finally:
        if had_alive:
            try:
                if pf.exists():
                    pf.unlink()
            except OSError:
                pass


def handle(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    conn = shop_db.get_conn()
    try:
        if action in ("get_state", "list_assets"):
            assets = shop_db.list_assets(conn, category=payload.get("category"))
            active_id = _opt_int(payload.get("asset_id"))
            bundle = shop_db.get_asset_bundle(conn, asset_id=active_id)
            low_stock = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM parts_inventory WHERE qty_on_hand <= min_qty ORDER BY part_name ASC"
                ).fetchall()
            ]
            return {
                "status": "ok",
                "assets": assets,
                "active": bundle,
                "low_stock_parts": low_stock,
                "db_path": shop_db.db_path(),
            }

        if action == "seed_demo":
            res = shop_db.seed_demo_shop(conn)
            return res

        if action == "save_asset":
            row = shop_db.save_asset(conn, payload)
            return {"status": "ok", "asset": row}

        if action == "delete_asset":
            ok = shop_db.delete_asset(conn, int(payload["id"]))
            return {"status": "ok", "deleted": ok}

        if action == "save_interval":
            row = shop_db.save_interval(conn, payload)
            return {"status": "ok", "interval": row}

        if action == "save_torque":
            row = shop_db.save_torque_spec(conn, payload)
            return {"status": "ok", "torque_spec": row}

        if action == "save_part":
            row = shop_db.save_part(conn, payload)
            return {"status": "ok", "part": row}

        if action == "save_log":
            row = shop_db.save_maintenance_log(conn, payload)
            return {"status": "ok", "log": row}

        if action == "upload_file":
            b64 = str(payload.get("content_b64") or "")
            fname = str(payload.get("filename") or "upload.bin")
            raw = base64.b64decode(b64)
            res = shop_db.save_upload(fname, raw)
            if payload.get("asset_id") and payload.get("set_asset_photo") and res["kind"] in ("jpg", "png", "webp"):
                conn.execute(
                    "UPDATE assets SET photo_path=?, updated_at=? WHERE id=?",
                    (res["file_path"], shop_db.now_ts(), int(payload["asset_id"])),
                )
                conn.commit()
            return res

        if action == "obd_discover":
            return obd_can.discover_adapters(scan_ble=bool(payload.get("scan_ble", True)))

        if action == "vin_decode":
            vin = str(payload.get("vin") or "")
            allow_net = bool(payload.get("allow_network", True))
            return shop_db.get_or_decode_vin(conn, vin, allow_network=allow_net)

        if action == "obd_query":
            t_q = str(payload.get("transport") or "sim")
            with _pause_collector_for_live_op(t_q):
                return obd_can.read_only_ecu_query(
                    transport=t_q,
                    port_or_addr=str(payload.get("port_or_addr") or ""),
                    commands=payload.get("commands") if isinstance(payload.get("commands"), list) else None,
                    ecu_header=str(payload.get("ecu_header") or ""),
                    discover_ecus=bool(payload.get("discover_ecus", True)),
                    baud=int(payload.get("baud") or 38400),
                )

        if action == "obd_scan":
            t_s = str(payload.get("transport") or "sim")
            aid_s = _opt_int(payload.get("asset_id"))
            with _pause_collector_for_live_op(t_s):
                res = obd_can.run_obd_scan(
                    transport=t_s,
                    port_or_addr=str(payload.get("port_or_addr") or ""),
                    sim_profile=str(payload.get("sim_profile") or "lean_misfire"),
                    baud=int(payload.get("baud") or 38400),
                )
            if res.get("status") == "ok" and res.get("vin"):
                vdec = shop_db.get_or_decode_vin(conn, res["vin"], allow_network=True)
                if vdec.get("status") == "ok":
                    res["vehicle_decoded"] = vdec
            if res.get("status") == "ok" and payload.get("save_session", True):
                sess = shop_db.save_obd_session(conn, {**res, "asset_id": aid_s})
                res["session_id"] = sess["id"]
            return res

        if action == "obd_lookup_dtc":
            codes = payload.get("codes")
            if isinstance(codes, str):
                codes = [c.strip() for c in codes.replace(",", " ").split() if c.strip()]
            elif not isinstance(codes, list):
                codes = [str(payload.get("code") or "")]
            return {"status": "ok", "results": [obd_can.lookup_dtc(c) for c in codes if c]}

        if action == "obd_clear_dtcs":
            # Always take a pre-clear snapshot if asset_id provided
            if payload.get("confirm_phrase") == "CLEAR_DTCS" and payload.get("transport", "sim") == "sim":
                pre = obd_can.run_obd_scan(transport="sim", sim_profile=str(payload.get("sim_profile") or "lean_misfire"))
                shop_db.save_obd_session(conn, {**pre, "asset_id": payload.get("asset_id"), "raw_notes": "Pre-clear safety snapshot"})
            return obd_can.clear_dtcs_guarded(
                transport=str(payload.get("transport") or "sim"),
                port_or_addr=str(payload.get("port_or_addr") or ""),
                confirm_phrase=str(payload.get("confirm_phrase") or ""),
                baud=int(payload.get("baud") or 38400),
            )

        if action == "obd_parse_freeze_frame":
            txt = str(payload.get("csv_text") or "")
            if not txt and payload.get("file_path"):
                fp = Path(str(payload["file_path"])).resolve()
                up = shop_db.upload_dir().resolve()
                if not str(fp).startswith(str(up) + os.sep):
                    return {"status": "error", "message": "file_path must reside inside shop-bench uploads directory"}
                txt = fp.read_text(encoding="utf-8", errors="replace")
            res = obd_can.parse_freeze_frame_csv(txt)
            if res.get("status") == "ok" and payload.get("save_session", True):
                sess = shop_db.save_obd_session(conn, {
                    "asset_id": payload.get("asset_id"),
                    "adapter_type": "csv_import",
                    "port_or_addr": str(payload.get("filename") or "freeze_frame.csv"),
                    "freeze_frame": res,
                    "raw_notes": "; ".join(res.get("anomalies") or []),
                })
                res["session_id"] = sess["id"]
            return res

        if action == "can_decode":
            candump = str(payload.get("candump_text") or "")
            dbc_text = str(payload.get("dbc_text") or "")
            if payload.get("sim_can_profile"):
                candump = obd_can.generate_sim_can_stream(str(payload["sim_can_profile"]))
            if payload.get("capture_interface"):
                res = obd_can.capture_socketcan(
                    interface=str(payload["capture_interface"]),
                    duration_sec=float(payload.get("duration_sec") or 2.0),
                    max_frames=int(payload.get("max_frames") or 100),
                    dbc_text=dbc_text,
                )
            else:
                res = obd_can.parse_candump_text(candump, dbc_text=dbc_text)
            if res.get("status") == "ok" and payload.get("save_capture", True):
                cap = shop_db.save_can_capture(conn, {
                    "asset_id": payload.get("asset_id"),
                    "interface": str(payload.get("capture_interface") or ("sim_can0" if payload.get("sim_can_profile") else "candump")),
                    "frame_count": res.get("frame_count", 0),
                    "unique_ids": res.get("unique_ids", []),
                    "decoded_signals": res.get("dbc_decoded") or res.get("obd_decoded") or [],
                    "summary": f"{res.get('frame_count', 0)} frames across {len(res.get('unique_ids', []))} CAN IDs",
                    "raw_sample": (candump or res.get("raw_sample") or "")[:4000],
                })
                res["capture_id"] = cap["id"]
                res["raw_sample"] = (candump or res.get("raw_sample") or "")[:4000]
            return res

        if action == "can_observe":
            # Direct CAN-bus observer: analyzes latest saved capture OR runs live SocketCAN / sim stream
            if payload.get("capture_interface") or payload.get("candump_text") or payload.get("sim_can_profile"):
                return handle("can_decode", payload)
            aid = int(payload["asset_id"]) if payload.get("asset_id") else None
            if aid:
                row = conn.execute("SELECT * FROM can_captures WHERE asset_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (aid,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM can_captures ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
            if not row:
                return {"status": "empty", "message": "No CAN-bus capture recorded yet. Paste a candump, run a live SocketCAN capture, or run a simulated CAN burst."}
            raw_s = row["raw_sample"] or ""
            res = obd_can.parse_candump_text(raw_s, dbc_text=str(payload.get("dbc_text") or ""))
            res["capture_id"] = row["id"]
            res["interface"] = row["interface"]
            return res

        if action == "telemetry_start_run":
            telemetry_collector.stop_existing_collector(wait_sec=1.5)
            aid_sr = _opt_int(payload.get("asset_id"))
            if payload.get("clear_buffer_first"):
                shop_db.clear_telemetry_data(conn, scope="buffer", asset_id=aid_sr)
            run_payload = dict(payload)
            run_payload["asset_id"] = aid_sr
            run = shop_db.create_telemetry_run(conn, run_payload)
            t_sr = str(payload.get("transport") or "sim").strip().lower()
            p_addr = str(payload.get("port_or_addr") or "").strip()
            collector_active = False
            collector_pid = 0
            if t_sr in ("tcp", "serial") and p_addr and bool(payload.get("use_collector", True)):
                sel_pids = payload.get("selected_pids") if isinstance(payload.get("selected_pids"), list) else shop_db.get_gauge_prefs(conn, asset_id=aid_sr)
                col_script = str(Path(__file__).resolve().parent / "telemetry_collector.py")
                cmd = [
                    sys.executable,
                    col_script,
                    "--run-id", str(run["id"]),
                    "--transport", t_sr,
                    "--port-or-addr", p_addr,
                    "--baud", str(int(payload.get("baud") or 38400)),
                    "--asset-id", str(aid_sr or ""),
                    "--selected-pids", json.dumps(sel_pids),
                ]
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                collector_active = True
                collector_pid = proc.pid
            return {
                "status": "ok",
                "run": run,
                "collector_active": collector_active,
                "collector_pid": collector_pid,
            }

        if action == "telemetry_stop_run":
            telemetry_collector.stop_existing_collector(wait_sec=2.0)
            return {"status": "ok", "stopped": True}

        if action == "clear_telemetry_data":
            sc = str(payload.get("scope") or "sim").strip().lower()
            if sc != "sim":
                telemetry_collector.stop_existing_collector(wait_sec=1.5)
            aid_cl = _opt_int(payload.get("asset_id"))
            return shop_db.clear_telemetry_data(conn, scope=sc, asset_id=aid_cl)

        if action == "telemetry_drain":
            telemetry_collector.touch_heartbeat()
            rid_d = _opt_int(payload.get("run_id"))
            after_id = _opt_int(payload.get("after_id")) or 0
            if not rid_d:
                return {"status": "error", "message": "run_id is required for telemetry_drain"}
            new_rows = shop_db.get_telemetry_samples_after(conn, rid_d, after_id=after_id, limit=120)
            st = telemetry_collector.read_status()
            alive = bool(st.get("alive")) and telemetry_collector.is_pid_alive(int(st.get("pid") or 0))
            buf_cnt = conn.execute("SELECT COUNT(*) FROM telemetry_samples WHERE run_id = ?", (rid_d,)).fetchone()[0]
            aid_d = _opt_int(payload.get("asset_id"))
            sup = shop_db.get_supported_pids(conn, asset_id=aid_d, port_or_addr=str(st.get("port_or_addr") or ""))
            return {
                "status": "ok",
                "run_id": rid_d,
                "samples": new_rows,
                "last_id": new_rows[-1]["id"] if new_rows else after_id,
                "buffered_samples": int(buf_cnt),
                "collector_alive": alive,
                "collector_state": st.get("state") or ("running" if alive else "stopped"),
                "collector_error": st.get("error") or "",
                "fast_frame_mode": bool(st.get("fast_frame_mode")),
                "avg_interval_sec": st.get("avg_interval_sec") or 0.0,
                "total_channels_captured": st.get("total_channels_captured") or 0,
                "vin": st.get("vin") or "",
                "vehicle_decoded": st.get("vehicle_decoded") or {},
                "supported_count": len(sup),
                "catalog": pid_catalog.list_catalog_metadata(sup) if (sup and after_id == 0) else [],
            }

        if action == "telemetry_slice":
            rid_sl = _opt_int(payload.get("run_id"))
            aid_sl = _opt_int(payload.get("asset_id"))
            if not rid_sl:
                runs = shop_db.list_telemetry_runs(conn, asset_id=aid_sl, limit=1)
                if not runs:
                    return {"status": "empty", "message": "No recorded telemetry runs yet."}
                rid_sl = runs[0]["id"]
            return shop_db.query_telemetry_slice(
                conn,
                run_id=rid_sl,
                sort_by=str(payload.get("sort_by") or "ts"),
                desc=bool(payload.get("desc", False)),
                filter_channel=str(payload.get("filter_channel") or ""),
                min_val=_opt_float(payload.get("min_val")),
                max_val=_opt_float(payload.get("max_val")),
                limit=_opt_int(payload.get("limit")) or 15,
            )

        if action == "telemetry_tick":
            run_id = _opt_int(payload.get("run_id"))
            aid = _opt_int(payload.get("asset_id"))
            port_addr = str(payload.get("port_or_addr") or "")
            cached_sup = shop_db.get_supported_pids(conn, asset_id=aid, port_or_addr=port_addr)
            custom_pids = shop_db.list_custom_pids(conn, asset_id=aid)
            tick = obd_can.sample_live_tick(
                transport=str(payload.get("transport") or "sim"),
                port_or_addr=port_addr,
                sim_profile=str(payload.get("sim_profile") or "vacuum_leak_drive"),
                tick_index=int(payload.get("tick_index") or 0),
                baud=int(payload.get("baud") or 38400),
                selected_pids=payload.get("selected_pids") if isinstance(payload.get("selected_pids"), list) else None,
                supported_pids=cached_sup or None,
                custom_pids=custom_pids or None,
            )
            if tick.get("status") != "ok":
                return tick
            if tick.get("discovered_supported_pids"):
                saved_sup = shop_db.save_supported_pids(conn, tick["discovered_supported_pids"], asset_id=aid, port_or_addr=port_addr)
                tick["catalog"] = pid_catalog.list_catalog_metadata(saved_sup)
            if run_id:
                shop_db.append_telemetry_sample(conn, int(run_id), tick, rolling_cap=600)
                tick["run_id"] = int(run_id)
            rich_cnt = sum(
                1 for k, v in tick.items()
                if k not in ("status", "ts", "run_id", "dtcs", "dtcs_checked", "pids_ok_count", "vin", "protocol", "extra_pids", "buffered_samples", "discovered_supported_pids", "catalog", "knock_deg")
                and isinstance(v, (int, float))
            )
            tick["total_channels_captured"] = rich_cnt
            return tick

        if action == "telemetry_record_burst":
            # Instantly record N samples (default 30) across a dynamic simulated drive cycle for immediate signal/correlation lab
            count = max(10, min(120, int(payload.get("count") or 30)))
            prof = str(payload.get("sim_profile") or "vacuum_leak_drive")
            run = shop_db.create_telemetry_run(conn, {
                **payload,
                "transport": "sim",
                "sim_profile": prof,
                "run_label": payload.get("run_label") or f"Drive Cycle ({prof})",
            })
            base_ts = shop_db.now_ts() - count * 2
            for i in range(count):
                s = obd_can.sample_live_tick(transport="sim", sim_profile=prof, tick_index=i)
                s["ts"] = float(base_ts + i * 2.0)
                shop_db.append_telemetry_sample(conn, run["id"], s)
            # Also generate matching CAN capture for brownout/CAN profiles
            if payload.get("include_can", True):
                raw_can = obd_can.generate_sim_can_stream(prof, count=36)
                parsed_can = obd_can.parse_candump_text(raw_can)
                shop_db.save_can_capture(conn, {
                    "asset_id": payload.get("asset_id"),
                    "interface": "sim_can0",
                    "frame_count": parsed_can.get("frame_count", 0),
                    "unique_ids": parsed_can.get("unique_ids", []),
                    "decoded_signals": parsed_can.get("obd_decoded", []),
                    "summary": f"Simulated CAN stream ({prof}): {parsed_can.get('frame_count', 0)} frames",
                    "raw_sample": raw_can,
                })
            return handle("telemetry_analyze", {"run_id": run["id"], "asset_id": payload.get("asset_id")})

        if action == "telemetry_list_runs":
            aid = int(payload["asset_id"]) if payload.get("asset_id") else None
            return {"status": "ok", "runs": shop_db.list_telemetry_runs(conn, asset_id=aid, limit=25)}

        if action == "telemetry_analyze":
            rid = _opt_int(payload.get("run_id"))
            aid = _opt_int(payload.get("asset_id"))
            if not rid:
                runs = shop_db.list_telemetry_runs(conn, asset_id=aid, limit=1)
                if not runs:
                    return {"status": "empty", "message": "No recorded telemetry runs yet. Start a Live Stream or Record Drive Cycle first."}
                rid = runs[0]["id"]
            run, samples = shop_db.get_telemetry_run_samples(conn, int(rid))
            if not run:
                return {"status": "error", "message": f"Telemetry run {rid} not found"}
            asset_meta = {
                "asset_id": run.get("asset_id"),
                "year": run.get("vehicle_year"),
                "make": run.get("vehicle_make"),
                "model": run.get("vehicle_model"),
                "vin": run.get("vin"),
                "run_id": run["id"],
                "run_label": run["run_label"],
                "transport": run["transport"],
            }
            if run.get("asset_id"):
                ar = conn.execute("SELECT name, engine_spec, meter_value, meter_unit, vin_or_serial FROM assets WHERE id = ?", (int(run["asset_id"]),)).fetchone()
                if ar:
                    asset_meta.update(dict(ar))
                    if not asset_meta.get("vin") and ar["vin_or_serial"]:
                        asset_meta["vin"] = ar["vin_or_serial"]
            if asset_meta.get("vin"):
                vdec = shop_db.get_or_decode_vin(conn, str(asset_meta["vin"]), allow_network=True)
                if vdec.get("status") == "ok":
                    asset_meta["vehicle_decoded"] = vdec
            hist = shop_db.collect_historical_dtcs(conn, asset_id=run.get("asset_id"), include_sim=(run.get("transport") == "sim"))
            analysis = signal_analytics.analyze_telemetry_run(samples, asset_meta=asset_meta, historical_dtcs=hist)
            analysis["run"] = run
            return analysis

        if action == "pid_catalog":
            aid = _opt_int(payload.get("asset_id"))
            cached_sup = shop_db.get_supported_pids(conn, asset_id=aid, port_or_addr=str(payload.get("port_or_addr") or ""))
            return {
                "status": "ok",
                "standard_pids": pid_catalog.list_catalog_metadata(cached_sup or None),
                "supported_pids": cached_sup,
                "custom_pids": shop_db.list_custom_pids(conn, asset_id=aid),
                "gauge_prefs": shop_db.get_gauge_prefs(conn, asset_id=aid),
            }

        if action == "pid_probe_supported":
            port_addr = str(payload.get("port_or_addr") or "")
            t_p = str(payload.get("transport") or "sim")
            with _pause_collector_for_live_op(t_p):
                res = obd_can.probe_supported_pids(
                    transport=t_p,
                    port_or_addr=port_addr,
                    baud=int(payload.get("baud") or 38400),
                )
            aid = _opt_int(payload.get("asset_id"))
            if res.get("status") == "ok" and res.get("supported_pids"):
                shop_db.save_supported_pids(conn, res["supported_pids"], asset_id=aid, port_or_addr=port_addr)
            res["custom_pids"] = shop_db.list_custom_pids(conn, asset_id=aid)
            res["gauge_prefs"] = shop_db.get_gauge_prefs(conn, asset_id=aid)
            return res

        if action == "import_torque_csv":
            parsed = pid_catalog.parse_torque_csv(str(payload.get("csv_text") or ""))
            if not parsed:
                return {"status": "error", "message": "No valid Torque Pro PID rows found in CSV (need Name, ModeAndPID, Equation)."}
            aid = int(payload["asset_id"]) if payload.get("asset_id") else None
            added = shop_db.save_custom_pids(conn, parsed, asset_id=aid)
            return {"status": "ok", "imported_count": added, "custom_pids": shop_db.list_custom_pids(conn, asset_id=aid)}

        if action == "import_obdb_json":
            parsed = pid_catalog.parse_obdb_json(str(payload.get("json_text") or ""))
            if not parsed:
                return {"status": "error", "message": "No valid OBDb signals found in JSON."}
            aid = int(payload["asset_id"]) if payload.get("asset_id") else None
            added = shop_db.save_custom_pids(conn, parsed, asset_id=aid)
            return {"status": "ok", "imported_count": added, "custom_pids": shop_db.list_custom_pids(conn, asset_id=aid)}

        if action == "delete_custom_pid":
            ok = shop_db.delete_custom_pid(conn, int(payload["id"]))
            return {"status": "ok", "deleted": ok}

        if action == "delete_dbc_profile":
            ok = shop_db.delete_dbc_profile(conn, int(payload["id"]))
            return {"status": "ok", "deleted": ok}

        if action == "save_dbc_profile":
            dbc_txt = str(payload.get("dbc_text") or "")
            msgs = obd_can.parse_dbc(dbc_txt)
            if not msgs:
                return {"status": "error", "message": "No valid BO_/SG_ CAN messages found in DBC text."}
            aid = int(payload["asset_id"]) if payload.get("asset_id") else None
            prof = shop_db.save_dbc_profile(
                conn,
                name=str(payload.get("name") or "Vehicle DBC"),
                dbc_text=dbc_txt,
                message_count=len(msgs),
                asset_id=aid,
                source=str(payload.get("source") or "opendbc"),
            )
            return {"status": "ok", "profile": prof, "message_count": len(msgs)}

        if action == "save_gauge_prefs":
            aid = int(payload["asset_id"]) if payload.get("asset_id") else None
            saved = shop_db.save_gauge_prefs(conn, list(payload.get("keys") or []), asset_id=aid)
            return {"status": "ok", "gauge_prefs": saved}

        if action == "ble_gatt_probe":
            return obd_can.probe_ble_gatt(
                address=str(payload.get("address") or ""),
                scan_timeout=float(payload.get("timeout") or 4.0),
            )

        if action == "elm_can_sniff":
            aid = _opt_int(payload.get("asset_id"))
            t_sn = str(payload.get("transport") or "sim")
            dbc_txt = str(payload.get("dbc_text") or "") or shop_db.get_active_dbc_text(conn, asset_id=aid)
            with _pause_collector_for_live_op(t_sn):
                res = obd_can.elm_sniff_can(
                    transport=t_sn,
                    port_or_addr=str(payload.get("port_or_addr") or ""),
                    filter_can_id=str(payload.get("filter_can_id") or ""),
                    duration_sec=float(payload.get("duration_sec") or 2.5),
                    dbc_text=dbc_txt,
                    baud=int(payload.get("baud") or 38400),
                )
            if res.get("status") == "ok" and res.get("frame_count", 0) > 0 and payload.get("save_capture", True):
                cap = shop_db.save_can_capture(conn, {
                    "asset_id": aid,
                    "interface": f"elm_{res.get('transport', 'sim')}",
                    "frame_count": res.get("frame_count", 0),
                    "unique_ids": res.get("unique_ids", []),
                    "decoded_signals": res.get("dbc_decoded") or res.get("obd_decoded") or [],
                    "summary": f"ELM327 ATMA sniff ({res.get('filter_can_id') or 'ALL'}): {res.get('frame_count', 0)} frames",
                    "raw_sample": res.get("raw_sample", ""),
                })
                res["capture_id"] = cap["id"]
            return res

        if action == "bt_bridge_status":
            return bt_bridge.bridge_status(
                com_port=payload.get("com_port"),
                tcp_port=payload.get("tcp_port"),
                bind_address=payload.get("bind_address"),
                probe_tcp=bool(payload.get("probe_tcp", False)),
                target_host=payload.get("target_host"),
            )

        if action == "bt_bridge_list_com":
            return bt_bridge.list_windows_com_ports()

        if action == "bt_bridge_start":
            return bt_bridge.start_bridge(
                com_port=payload.get("com_port", "COM5"),
                tcp_port=payload.get("tcp_port", 35000),
                bind_address=payload.get("bind_address"),
                baud_rate=payload.get("baud_rate", 38400),
                target_host=payload.get("target_host"),
            )

        if action == "bt_bridge_stop":
            return bt_bridge.stop_bridge(tcp_port=payload.get("tcp_port"))

        if action == "bt_bridge_test":
            return bt_bridge.test_tcp_link(
                tcp_url=str(payload.get("tcp_url") or payload.get("port_or_addr") or "tcp://127.0.0.1:35000"),
                probe_elm=bool(payload.get("probe_elm", True)),
            )

        if action == "export_report":
            aid = int(payload["asset_id"]) if payload.get("asset_id") else None
            rid = int(payload["run_id"]) if payload.get("run_id") else None
            return report_export.build_diagnostic_report(
                conn,
                asset_id=aid,
                run_id=rid,
                fmt=str(payload.get("format") or "markdown"),
                save_file=bool(payload.get("save_file", True)),
            )

        return {"status": "error", "message": f"Unknown shop-bench action: {action}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "get_state"
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}
    res = handle(action, payload)
    print(json.dumps(res, indent=2))
    return 0 if res.get("status") != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
