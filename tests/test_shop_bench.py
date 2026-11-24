#!/usr/bin/env python3
"""Unit & integration tests for mod-shop-bench v2.6 (Pro Scan-Tool, 45+ PIDs, Torque CSV/OBDb/DBC, AST safety, TCP SSRF guard, BT Bridge controller, Report Export & Signal Lab)."""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

MOD_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MOD_DIR / "tools"))

import bt_bridge  # noqa: E402
import obd_can  # noqa: E402
import pid_catalog  # noqa: E402
import shop_cli  # noqa: E402


def run_all() -> None:
    with tempfile.TemporaryDirectory(prefix="shop-bench-test-") as tmp:
        os.environ["MODULE_DATA_DIR"] = tmp

        # 1. Seed & ledger CRUD
        s = shop_cli.handle("seed_demo", {})
        assert s["status"] == "ok" and s["seeded"] is True
        st = shop_cli.handle("get_state", {})
        assert len(st["assets"]) == 2
        aid = st["active"]["asset"]["id"]

        # 2. 45+ SAE J1979 PID catalog & supported-PID bitmask decoding
        pr = shop_cli.handle("pid_probe_supported", {"transport": "sim", "asset_id": aid})
        assert pr["status"] == "ok" and pr["supported_count"] >= 40
        # Bitmask test: 0xBE3FA813 -> PID 01, 03, 04, 05, 06, 07...
        decoded_mask = pid_catalog.decode_supported_pid_bitmask("00", [0xBE, 0x3F, 0xA8, 0x13])
        assert "01" in decoded_mask and "04" in decoded_mask and "0C" in decoded_mask and "02" not in decoded_mask

        # 3. Safe AST PID equation evaluator (must allow math & block code injection)
        assert pid_catalog.evaluate_pid_equation("((A*256)+B)/4", [0x1A, 0xF8]) == 1726.0
        assert pid_catalog.evaluate_pid_equation("Signed(A)*0.5", [0xFE]) == -1.0
        blocked_ast = False
        try:
            pid_catalog.evaluate_pid_equation("__import__('os').system('id')", [1, 2])
        except ValueError:
            blocked_ast = True
        assert blocked_ast is True

        # 4. Torque Pro CSV & OBDb JSON extended PID importers + gauge preferences
        torque_csv = "Name,ShortName,ModeAndPID,Equation,Min Value,Max Value,Units,Header\nTrans Temp,TFT,221E1C,((A*256)+B)*0.0625-40,-40,150,C,7E1\n"
        t_res = shop_cli.handle("import_torque_csv", {"csv_text": torque_csv, "asset_id": aid})
        assert t_res["status"] == "ok" and t_res["imported_count"] == 1

        obdb_json = json.dumps({"commands": [{"hdr": "7E0", "cmd": {"22": "115C"}, "signals": [{"id": "OIL_P", "name": "Oil Pressure", "fmt": {"bix": 0, "len": 8, "mul": 4, "div": 1, "add": 0, "unit": "kPa"}}]}]})
        o_res = shop_cli.handle("import_obdb_json", {"json_text": obdb_json, "asset_id": aid})
        assert o_res["status"] == "ok" and o_res["imported_count"] == 1

        gp = shop_cli.handle("save_gauge_prefs", {"asset_id": aid, "keys": ["rpm", "battery_v", "oil_temp_c", "custom:TFT"]})
        assert gp["status"] == "ok" and "custom:TFT" in gp["gauge_prefs"]

        # 5. DBC profile store & ELM327 filtered CAN sniff (ATH1/ATCRA/ATMA)
        dbc_txt = 'BO_ 513 ENGINE_DATA: 8 ECM\n SG_ EngineSpeed : 0|16@1+ (0.25,0) [0|8000] "rpm" Vector__XXX\n'
        dp = shop_cli.handle("save_dbc_profile", {"name": "demo_opendbc.dbc", "dbc_text": dbc_txt, "asset_id": aid})
        assert dp["status"] == "ok" and dp["message_count"] == 1

        sn = shop_cli.handle("elm_can_sniff", {"transport": "sim", "asset_id": aid})
        assert sn["status"] == "ok" and sn["frame_count"] > 50 and len(sn["bus_analysis"]["dropout_alerts"]) >= 1
        sn_filt = shop_cli.handle("elm_can_sniff", {"transport": "sim", "filter_can_id": "201", "asset_id": aid})
        assert sn_filt["status"] == "ok" and sn_filt["bus_analysis"]["unique_id_count"] == 1

        # 6. TCP ELM327 SSRF guard (allow localhost/host.docker.internal/RFC1918; block metadata/public)
        assert obd_can._validate_elm_tcp_target("tcp://host.docker.internal:35000") == ("host.docker.internal", 35000)
        assert obd_can._validate_elm_tcp_target("tcp://192.168.0.10:35000") == ("192.168.0.10", 35000)
        for bad_t in ("tcp://169.254.169.254:80", "tcp://8.8.8.8:35000"):
            rejected = False
            try:
                obd_can._validate_elm_tcp_target(bad_t)
            except ValueError:
                rejected = True
            assert rejected is True

        # 7. Time-series drive-cycle & signal correlation signatures
        vac = shop_cli.handle("telemetry_record_burst", {"asset_id": aid, "sim_profile": "vacuum_leak_drive", "count": 30})
        assert any(sg["code"] == "VACUUM_LEAK_SIGNATURE" for sg in vac["signatures"])
        alt = shop_cli.handle("telemetry_record_burst", {"asset_id": aid, "sim_profile": "alternator_ripple_brownout", "count": 30})
        assert alt["voltage_health"]["status"] == "FAULT"
        cat = shop_cli.handle("telemetry_record_burst", {"asset_id": aid, "sim_profile": "catalyst_degraded_o2", "count": 30, "include_can": False})
        assert cat["o2_catalyst_health"]["status"] == "CATALYST_DEGRADED"

        # 8. Upload magic-byte + guarded Mode 04 clear
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        assert shop_cli.handle("upload_file", {"filename": "../../etc/p.png", "content_b64": base64.b64encode(png_bytes).decode()})["status"] == "ok"
        assert shop_cli.handle("upload_file", {"filename": "bad.png", "content_b64": base64.b64encode(b"#!/bin/sh").decode()})["status"] == "error"
        assert shop_cli.handle("obd_clear_dtcs", {"transport": "sim", "confirm_phrase": "no"})["status"] == "blocked"
        assert shop_cli.handle("obd_clear_dtcs", {"transport": "sim", "confirm_phrase": "CLEAR_DTCS"})["cleared"] is True

        # 9. Windows BT-to-TCP Bridge input validation & status defaults
        assert bt_bridge.validate_com_port("com5") == "COM5"
        assert bt_bridge.validate_tcp_port("35000") == 35000
        for bad_com in ("COM0", "COM1234", "COM5; calc.exe", "/dev/ttyUSB0", "../COM5"):
            failed = False
            try:
                bt_bridge.validate_com_port(bad_com)
            except ValueError:
                failed = True
            assert failed is True, f"Should reject COM port {bad_com}"
        for bad_port in (80, 1023, 65536, "abc"):
            failed = False
            try:
                bt_bridge.validate_tcp_port(bad_port)
            except ValueError:
                failed = True
            assert failed is True, f"Should reject TCP port {bad_port}"
        for bad_bind in ("8.8.8.8", "127.0.0.1; id"):
            failed = False
            try:
                bt_bridge.validate_bind_address(bad_bind)
            except ValueError:
                failed = True
            assert failed is True

        bst = shop_cli.handle("bt_bridge_status", {})
        assert bst["status"] == "ok" and bst["com_port"] == "COM5" and bst["tcp_port"] == 35000
        assert "win_obd_bridge.ps1" in bst["commands"]["start_ps1"]
        assert "-Stop -TcpPort 35000" in bst["commands"]["stop_ps1"]

        # 10. Diagnostic & LLM Copilot Report Export (Markdown, JSON, Print-HTML + XSS escaping)
        shop_cli.handle("save_log", {
            "asset_id": aid,
            "log_type": "diagnostic_triage",
            "title": "Copilot Vacuum Leak Triage <script>alert(1)</script>",
            "summary": "Likely cracked intake boot causing +19% fuel trims at idle.",
            "checklist": ["Inspect PCV hose", "Smoke-test intake manifold"],
        })
        rep_md = shop_cli.handle("export_report", {"asset_id": aid, "format": "markdown"})
        assert rep_md["status"] == "ok" and rep_md["filename"].endswith(".md")
        assert Path(rep_md["file_path"]).is_file()
        assert "Likely cracked intake boot" in rep_md["content"]
        assert "Smoke-test intake manifold" in rep_md["content"]

        rep_html = shop_cli.handle("export_report", {"asset_id": aid, "format": "html"})
        assert rep_html["status"] == "ok" and rep_html["filename"].endswith(".html")
        assert "<script>alert(1)</script>" not in rep_html["content"]
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rep_html["content"]

        rep_json = shop_cli.handle("export_report", {"asset_id": aid, "format": "json"})
        assert rep_json["status"] == "ok" and rep_json["filename"].endswith(".json")
        parsed_j = json.loads(rep_json["content"])
        assert len(parsed_j["copilot_and_service_logs"]) >= 1

        # 11. v2.6.3 Real Carly/ELM327 regression tests: ATS0 spaceless, ATS1 spaced, multi-ECU, ATRV without 'V', ISO-TP VIN, TcpSocketELM327.cmd()
        assert obd_can._parse_hex_response_bytes("410C0A2E", "41", "0C") == [0x0A, 0x2E]
        assert obd_can._parse_hex_response_bytes("41 0C 0A 2E", "41", "0C") == [0x0A, 0x2E]
        assert obd_can._parse_hex_response_bytes("SEARCHING...\r41 05 82\r", "41", "05") == [0x82]
        assert obd_can._parse_hex_response_bytes("NO DATA", "41", "0C") == []
        # Multi-ECU 0100 bitmask should not corrupt single-line decode and should union via _parse_all_hex_response_bytes
        multi_0100 = "41 00 BE 7F B8 13\r41 00 80 00 00 01"
        assert obd_can._parse_hex_response_bytes(multi_0100, "41", "00") == [0xBE, 0x7F, 0xB8, 0x13]
        assert len(obd_can._parse_all_hex_response_bytes(multi_0100, "41", "00")) == 2
        # Carly ATRV without trailing 'V' and standard with 'V'
        assert obd_can._parse_voltage("14.1") == 14.1
        assert obd_can._parse_voltage("14.2V") == 14.2
        assert obd_can._parse_voltage("ATRV\r13.85 V\r") == 13.85
        # Multi-ECU DTC response (spaced + spaceless + concatenated zero-DTC '43 00 43 00' must NOT decode as C0300!)
        dtcs_spaced = obd_can._parse_dtc_response("43 01 01 71\r43 01 03 02", "43")
        assert {d["code"] for d in dtcs_spaced} == {"P0171", "P0302"}
        dtcs_nospace = obd_can._parse_dtc_response("43010171\r4300", "43")
        assert [d["code"] for d in dtcs_nospace] == ["P0171"]
        assert obd_can._parse_dtc_response("43 00\r43 00", "43") == []
        assert obd_can._parse_dtc_response("43 00 43 00", "43") == []
        assert obd_can._parse_dtc_response("47 00 47 00", "47") == []
        assert obd_can._parse_dtc_response("4A 00 4A 00", "4A") == []
        # 12. v2.6.4 Rolling 600-sample ring buffer + rich extra_pids carry-forward & signal analytics
        import shop_db
        conn_t = shop_db.get_conn()
        try:
            run_r = shop_db.create_telemetry_run(conn_t, {"asset_id": aid, "transport": "tcp", "run_label": "600-Buffer Test"})
            for idx_s in range(615):
                ep = {"oil_temp_c": 95.0 + (idx_s % 5)} if idx_s % 2 == 0 else {"fuel_level_pct": 62.5}
                shop_db.append_telemetry_sample(conn_t, run_r["id"], {
                    "ts": 1000.0 + idx_s,
                    "rpm": 700.0 + idx_s,
                    "coolant_c": 90.0,
                    "battery_v": 14.1,
                    "extra_pids": ep,
                    "dtcs_checked": False,
                }, rolling_cap=600)
            _, buffered = shop_db.get_telemetry_run_samples(conn_t, run_r["id"], limit=600)
            assert len(buffered) == 600, f"Expected 600 rolling samples, got {len(buffered)}"
            # Must contain the MOST RECENT 600 samples (rpm 715..1314), NOT the oldest 600 (700..1299)!
            assert buffered[0]["rpm"] == 715.0 and buffered[-1]["rpm"] == 1314.0
            # Carry-forward must preserve BOTH oil_temp_c and fuel_level_pct on latest sample!
            assert buffered[-1]["extra_pids"].get("oil_temp_c") is not None
            assert buffered[-1]["extra_pids"].get("fuel_level_pct") == 62.5
            an_rich = shop_cli.handle("telemetry_analyze", {"run_id": run_r["id"], "asset_id": aid})
            assert an_rich["status"] == "ok" and an_rich["buffer_window"]["buffered_samples"] == 600
            assert "oil_temp_c" in an_rich["channels"] and "fuel_level_pct" in an_rich["channels"]
            assert "regime_snapshots" in an_rich and "peak_rpm" in an_rich["regime_snapshots"]
        finally:
            conn_t.close()

        # 13. v2.6.6 Regression Tests: VIN decode offline, _parse_voltage ISO-15765 guard, empty asset_id='',
        #     fuel-pressure-aware trim logic, telemetry_slice, telemetry_drain, and strictly read-only obd_query
        assert obd_can._parse_voltage("ISO 15765-4 CAN 11/500") == 0.0, "Must not match '15' in ISO 15765!"
        assert obd_can._parse_voltage("13.7") == 13.7 and obd_can._parse_voltage("14.12V") == 14.12
        # Empty asset_id string must not throw int('') error
        gs_empty = shop_cli.handle("get_state", {"asset_id": ""})
        assert gs_empty["status"] == "ok"
        # Offline VIN decode (standard synthetic test VIN)
        vd = shop_cli.handle("vin_decode", {"vin": "1HGCM82633A004352", "allow_network": False})
        assert vd["status"] == "ok" and vd["year"] == "2003" and vd["make"] == "Honda"
        # Strictly read-only multi-ECU query blocks Mode 04 clear & UDS 2E write!
        assert shop_cli.handle("obd_query", {"transport": "sim", "commands": ["04"]})["status"] == "blocked"
        assert shop_cli.handle("obd_query", {"transport": "sim", "commands": ["2E1234"]})["status"] == "blocked"
        oq_ok = shop_cli.handle("obd_query", {"transport": "sim", "commands": ["0101", "010C"]})
        assert oq_ok["status"] == "ok" and len(oq_ok["ecus_discovered"]) >= 2
        # Mode 01 PID 01 readiness decoder
        rd = obd_can.decode_mode01_readiness([0x00, 0x07, 0xE5, 0x04])
        assert rd["mil_on"] is False and rd["misfire"] == "READY" and rd["evap"] == "NOT_READY"
        # Fuel-pressure-aware trim signature: high trim vs RPM with STABLE fuel pressure must NOT claim starvation!
        import signal_analytics
        fp_samples = []
        for idx_fp in range(25):
            rpm_v = 650.0 + idx_fp * 100.0
            fp_samples.append({
                "ts": 2000.0 + idx_fp * 2.0,
                "rpm": rpm_v,
                "load_pct": 25.0 - idx_fp * 0.5,
                "map_kpa": 35.0,
                "maf_gps": 2.5 + idx_fp * 0.4,
                "stft_b1": 1.0 + idx_fp * 0.3,
                "ltft_b1": 7.0 + idx_fp * 0.45,
                "battery_v": 13.7,
                "extra_pids": {"fuel_press_kpa": 408.0, "frp_gdi_kpa": 2400.0 + idx_fp * 40.0},
            })
        fp_an = signal_analytics.analyze_telemetry_run(fp_samples)
        sig_codes = [sg["code"] for sg in fp_an["signatures"]]
        assert "HIGH_RPM_LEAN_TRIM_WITH_STABLE_FUEL_PRESSURE" in sig_codes, f"Expected falsification signature, got {sig_codes}"
        assert "FUEL_STARVATION_UNDER_LOAD" not in sig_codes
        assert fp_an["regime_snapshots"]["peak_rpm"]["fuel_press_kpa"] == 408.0
        # telemetry_slice & telemetry_drain checks
        sl = shop_cli.handle("telemetry_slice", {"run_id": run_r["id"], "sort_by": "rpm", "desc": True, "limit": 5})
        assert sl["status"] == "ok" and len(sl["rows"]) == 5 and sl["rows"][0]["rpm"] == 1314.0
        dr = shop_cli.handle("telemetry_drain", {"run_id": run_r["id"], "after_id": 0})
        assert dr["status"] == "ok" and len(dr["samples"]) > 0 and dr["buffered_samples"] == 600

        # 14. v2.6.9 Sim-DTC isolation & clear_telemetry_data + delete_dbc_profile
        conn_iso = shop_db.get_conn()
        try:
            real_hist = shop_db.collect_historical_dtcs(conn_iso, asset_id=aid, include_sim=False)
            assert "P0171" not in real_hist and "U0100" not in real_hist, f"Sim codes leaked into real vehicle history: {real_hist}"
            sim_hist = shop_db.collect_historical_dtcs(conn_iso, asset_id=aid, include_sim=True)
            assert len(sim_hist) >= 1
        finally:
            conn_iso.close()
        cl_sim = shop_cli.handle("clear_telemetry_data", {"scope": "sim", "asset_id": aid})
        assert cl_sim["status"] == "ok" and cl_sim["deleted_runs"] >= 1
        cl_buf = shop_cli.handle("clear_telemetry_data", {"scope": "buffer", "asset_id": aid})
        assert cl_buf["status"] == "ok" and cl_buf["deleted_runs"] >= 1
        assert shop_cli.handle("delete_dbc_profile", {"id": dp["profile"]["id"]})["deleted"] is True
        # ISO-TP multi-frame VIN (standard 3-frame format)
        raw_vin = "014\r0: 49 02 01 31 48 47\r1: 43 4D 38 32 36 33 33\r2: 41 30 30 34 33 35 32"
        assert obd_can._parse_vin_response(raw_vin) == "1HGCM82633A004352"
        # End-to-end TCP mock ELM327 server verifying test_tcp_link & fast sample_live_tick (no ATZ per tick!)
        import socket as _sock
        import threading as _thr
        seen_cmds = []
        srv = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        mport = srv.getsockname()[1]
        srv.listen(4)
        srv.settimeout(3.0)

        def _mock_elm_loop():
            canned = {
                "ATE0": "OK",
                "ATL0": "OK",
                "ATS1": "OK",
                "ATAT2": "OK",
                "ATST14": "OK",
                "ATI": "ELM327 v1.4 v18",
                "ATDP": "AUTO, ISO 15765-4 (CAN 11/500)",
                "ATRV": "14.1",
                "0100": "SEARCHING...\r41 00 BE 7F B8 13\r41 00 80 00 00 01",
                "0120": "41 20 80 00 00 01",
                "0140": "41 40 40 00 00 00",
                "0160": "41 60 01 00 00 00",
                "010C": "41 0C 0A 2E",
                "0105": "41 05 82",
                "0104": "41 04 49",
                "0106": "41 06 82",
                "0107": "41 07 84",
                "0111": "41 11 25",
                "010D": "41 0D 00",
                "0110": "41 10 02 1E",
                "010B": "41 0B 21",
                "0114": "41 14 5A FF",
                "0115": "41 15 70 FF",
                "010E": "41 0E 9A",
                "010F": "41 0F 47",
                "0142": "41 42 37 78",
                "03": "43 01 01 71",
                "07": "47 00",
            }
            for _ in range(8):
                try:
                    c, _ = srv.accept()
                except Exception:
                    break
                c.settimeout(1.5)
                buf = b""
                try:
                    while True:
                        ch = c.recv(256)
                        if not ch:
                            break
                        buf += ch
                        while b"\r" in buf:
                            line, buf = buf.split(b"\r", 1)
                            cmd = line.decode("ascii", errors="ignore").strip().upper()
                            if not cmd:
                                continue
                            seen_cmds.append(cmd)
                            resp = canned.get(cmd, "NO DATA")
                            c.sendall((resp + "\r\r>").encode("ascii"))
                except Exception:
                    pass
                finally:
                    c.close()

        th = _thr.Thread(target=_mock_elm_loop, daemon=True)
        th.start()
        try:
            tl = bt_bridge.test_tcp_link(f"tcp://127.0.0.1:{mport}", probe_elm=True)
            assert tl["status"] == "ok" and tl["elm_ready"] is True and "ELM327" in tl["adapter_id"]
            t0 = obd_can.sample_live_tick("tcp", f"tcp://127.0.0.1:{mport}", tick_index=0, selected_pids=["rpm", "battery_v", "coolant_c", "total_ft_b1", "load_pct"])
            assert t0["status"] == "ok" and t0["rpm"] == 651.5 and t0["coolant_c"] == 90.0 and t0["battery_v"] == 14.1
            assert t0["load_pct"] is not None and t0["stft_b1"] is not None and t0["ltft_b1"] is not None
            assert t0["dtcs_checked"] is True and len(t0["dtcs"]) == 1 and t0["dtcs"][0]["code"] == "P0171"
            t1 = obd_can.sample_live_tick("tcp", f"tcp://127.0.0.1:{mport}", tick_index=1, selected_pids=["rpm", "battery_v", "coolant_c"])
            assert t1["status"] == "ok" and t1["rpm"] == 651.5 and t1["coolant_c"] == 90.0 and t1["dtcs_checked"] is False
            assert "ATZ" not in seen_cmds, f"Live ticks must NEVER send slow ATZ reset! Got: {seen_cmds}"
        finally:
            srv.close()
            th.join(timeout=2.0)

        print("ALL_SHOP_BENCH_V26_TESTS_PASS")


if __name__ == "__main__":
    run_all()
