#!/usr/bin/env python3
"""
tools/telemetry_collector.py — Persistent "Buffer & Drain" Background OBD-II Telemetry Collector (v2.6.6).

Why this exists:
  Instead of spawning a fresh Python process + opening/closing a TCP socket on every browser poll
  (~400ms overhead + 200ms/PID wait = ~3-4s/sample), this lightweight collector holds ONE persistent
  ELM327 connection (TCP bridge or serial), auto-probes the ELM327 '1' single-frame response hint
  (010C1 -> ~35-55ms/PID, with automatic clone fallback to plain 010C), continuously writes rich
  multi-channel samples into the SQLite 600-sample rolling ring buffer at ~1-2.5 Hz, and lets the
  UI simply drain new rows every 600ms (<15ms SQLite read!).

Safety & Lifecycle:
  - Writes PID to collector.pid, honors collector.stop, and pauses cleanly if collector.pause exists
    (so live on-demand Copilot scans / PID discovery never contend on the single bridge socket!).
  - Auto-exits cleanly if the browser stops draining (heartbeat > 45s stale), preventing orphans.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import obd_can  # noqa: E402
import shop_db  # noqa: E402
import vin_decoder  # noqa: E402


def _state_dir() -> Path:
    d = shop_db.data_dir() / "collector"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file() -> Path:
    return _state_dir() / "collector.pid"


def stop_file() -> Path:
    return _state_dir() / "collector.stop"


def pause_file() -> Path:
    return _state_dir() / "collector.pause"


def heartbeat_file() -> Path:
    return _state_dir() / "collector.heartbeat"


def status_file() -> Path:
    return _state_dir() / "collector.status.json"


def touch_heartbeat() -> None:
    try:
        heartbeat_file().write_text(str(time.time()), encoding="ascii")
    except OSError:
        pass


def write_status(data: Dict[str, Any]) -> None:
    try:
        tmp = status_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(status_file())
    except OSError:
        pass


def read_status() -> Dict[str, Any]:
    sf = status_file()
    if not sf.is_file():
        return {}
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_existing_collector(wait_sec: float = 2.0) -> None:
    try:
        stop_file().write_text("stop", encoding="ascii")
    except OSError:
        pass
    pf = pid_file()
    old_pid = 0
    if pf.is_file():
        txt = pf.read_text(encoding="ascii", errors="ignore").strip()
        if txt.isdigit():
            old_pid = int(txt)
    deadline = time.time() + wait_sec
    while old_pid > 0 and is_pid_alive(old_pid) and time.time() < deadline:
        time.sleep(0.1)
    if old_pid > 0 and is_pid_alive(old_pid):
        try:
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(0.2)
        except OSError:
            pass
    for f in (pf, stop_file()):
        try:
            if f.exists():
                f.unlink()
        except OSError:
            pass


def _open_transport(transport: str, port_or_addr: str, baud: int) -> Any:
    if transport == "tcp":
        elm = obd_can.TcpSocketELM327(port_or_addr, timeout=1.5)
        elm.open()
        return elm
    if transport == "serial":
        elm = obd_can.PosixSerialELM327(port_or_addr, baud=baud, timeout=1.5)
        elm.open()
        return elm
    raise ValueError(f"Collector supports tcp or serial transports (got '{transport}')")


def run_collector(
    run_id: int,
    transport: str,
    port_or_addr: str,
    baud: int,
    asset_id: Optional[int],
    selected_pids: List[str],
    idle_timeout_sec: float = 45.0,
) -> int:
    stop_flag = False

    def _on_sig(_signum: int, _frame: Any) -> None:
        nonlocal stop_flag
        stop_flag = True

    signal.signal(signal.SIGTERM, _on_sig)
    signal.signal(signal.SIGINT, _on_sig)

    try:
        if stop_file().exists():
            stop_file().unlink()
    except OSError:
        pass
    pid_file().write_text(str(os.getpid()), encoding="ascii")
    touch_heartbeat()

    conn = shop_db.get_conn()
    elm: Any = None
    fast_frame = False
    supported_pids = shop_db.get_supported_pids(conn, asset_id=asset_id, port_or_addr=port_or_addr)
    custom_pids = shop_db.list_custom_pids(conn, asset_id=asset_id)
    vin_str = ""
    decoded_vehicle: Dict[str, Any] = {}
    tick_idx = 0
    err_streak = 0
    last_sample_ts = 0.0
    avg_interval = 0.0
    buffered = 0
    total_channels = 0

    def _update_stat(state_txt: str, err: str = "") -> None:
        write_status({
            "alive": state_txt in ("starting", "running", "paused"),
            "state": state_txt,
            "pid": os.getpid(),
            "run_id": run_id,
            "transport": transport,
            "port_or_addr": port_or_addr,
            "fast_frame_mode": fast_frame,
            "supported_count": len(supported_pids),
            "buffered_samples": buffered,
            "total_channels_captured": total_channels,
            "avg_interval_sec": round(avg_interval, 2),
            "vin": vin_str,
            "vehicle_decoded": decoded_vehicle,
            "error": err,
            "updated_at": time.time(),
        })

    _update_stat("starting")
    try:
        while not stop_flag and not stop_file().exists():
            # Idle heartbeat guard so closing the browser tab never leaves an orphan collector running
            try:
                hb = float(heartbeat_file().read_text(encoding="ascii").strip())
                if time.time() - hb > idle_timeout_sec:
                    break
            except Exception:
                pass

            # Yield bridge socket cleanly while an on-demand Copilot scan / PID discovery is running!
            if pause_file().exists():
                if elm is not None:
                    try:
                        elm.close()
                    except Exception:
                        pass
                    elm = None
                _update_stat("paused")
                time.sleep(0.15)
                continue

            if elm is None:
                try:
                    elm = _open_transport(transport, port_or_addr, baud)
                    for init_c in ("ATE0", "ATL0", "ATS1", "ATAT2", "ATST14"):
                        elm.send(init_c)
                    if not supported_pids:
                        disc = obd_can._discover_supported_on_open_elm(elm)
                        if disc:
                            supported_pids = shop_db.save_supported_pids(
                                conn, disc, asset_id=asset_id, port_or_addr=port_or_addr
                            )
                    if not vin_str:
                        raw_v = elm.send("0902")
                        vin_str = obd_can._parse_vin_response(raw_v)
                        if vin_str:
                            decoded_vehicle = shop_db.get_or_decode_vin(conn, vin_str)
                            shop_db.update_telemetry_run_vehicle(
                                conn,
                                run_id,
                                vin=vin_str,
                                year=decoded_vehicle.get("year", ""),
                                make=decoded_vehicle.get("make", ""),
                                model=decoded_vehicle.get("model", ""),
                            )
                    fast_frame = obd_can.probe_fast_frame_support(elm)
                    err_streak = 0
                except Exception as exc:
                    err_streak += 1
                    _update_stat("starting", err=str(exc))
                    if elm is not None:
                        try:
                            elm.close()
                        except Exception:
                            pass
                        elm = None
                    if err_streak >= 5:
                        _update_stat("error", err=f"Collector stopped after 5 connect errors: {exc}")
                        break
                    time.sleep(1.0)
                    continue

            t0 = time.time()
            try:
                tick = obd_can.poll_live_tick_on_elm(
                    elm=elm,
                    tick_index=tick_idx,
                    selected_pids=selected_pids,
                    supported_pids=supported_pids or None,
                    custom_pids=custom_pids or None,
                    use_fast_suffix=fast_frame,
                )
            except Exception as exc:
                err_streak += 1
                _update_stat("running", err=str(exc))
                try:
                    elm.close()
                except Exception:
                    pass
                elm = None
                if err_streak >= 5:
                    _update_stat("error", err=f"Collector stopped after 5 I/O errors: {exc}")
                    break
                time.sleep(0.6)
                continue

            if tick.get("status") != "ok":
                err_streak += 1
                _update_stat("running", err=str(tick.get("message") or "Empty tick"))
                if err_streak >= 4:
                    try:
                        elm.close()
                    except Exception:
                        pass
                    elm = None
                time.sleep(0.5)
                continue

            err_streak = 0
            tick_idx += 1
            if vin_str and not tick.get("vin"):
                tick["vin"] = vin_str
            shop_db.append_telemetry_sample(conn, run_id, tick, rolling_cap=600)
            buffered = int(tick.get("buffered_samples") or buffered + 1)
            total_channels = sum(
                1 for k, v in tick.items()
                if k not in ("status", "ts", "run_id", "dtcs", "dtcs_checked", "pids_ok_count", "vin", "protocol", "extra_pids", "buffered_samples", "discovered_supported_pids", "catalog", "knock_deg")
                and isinstance(v, (int, float))
            )
            now_t = float(tick.get("ts") or time.time())
            if last_sample_ts > 0:
                dt = max(0.05, now_t - last_sample_ts)
                avg_interval = dt if avg_interval <= 0 else (avg_interval * 0.7 + dt * 0.3)
            last_sample_ts = now_t
            _update_stat("running")

            # Tiny breather so stop/pause flags react promptly while maximizing ECU cadence
            elapsed = time.time() - t0
            if elapsed < 0.15:
                time.sleep(0.15 - elapsed)
            else:
                time.sleep(0.04)
    finally:
        if elm is not None:
            try:
                elm.close()
            except Exception:
                pass
        conn.close()
        _update_stat("stopped")
        for f in (pid_file(), stop_file()):
            try:
                if f.exists():
                    f.unlink()
            except OSError:
                pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Shop Bench persistent OBD-II buffer-and-drain collector")
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--transport", required=True)
    ap.add_argument("--port-or-addr", required=True)
    ap.add_argument("--baud", type=int, default=38400)
    ap.add_argument("--asset-id", default="")
    ap.add_argument("--selected-pids", default="[]")
    args = ap.parse_args()
    aid = int(args.asset_id) if str(args.asset_id).strip().isdigit() else None
    try:
        sel = json.loads(args.selected_pids)
        if not isinstance(sel, list):
            sel = []
    except Exception:
        sel = []
    return run_collector(
        run_id=args.run_id,
        transport=args.transport.strip().lower(),
        port_or_addr=args.port_or_addr.strip(),
        baud=args.baud,
        asset_id=aid,
        selected_pids=[str(x) for x in sel],
    )


if __name__ == "__main__":
    sys.exit(main())
