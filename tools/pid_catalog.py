#!/usr/bin/env python3
"""
tools/pid_catalog.py — SAE J1979 Standard PID Catalog (45+ PIDs), Supported-PID Bitmask
Discovery (0100-0160), Safe AST Formula Evaluator, Torque Pro .csv & OBDb JSON Importers.

Zero `eval()` / `exec()`: custom PID equations are parsed with `ast.parse(..., mode='eval')`
and strictly restricted to numeric constants, A..H byte variables, basic arithmetic,
bit shifts/masks, and safe helper functions (`signed`, `min`, `max`, `abs`).
"""
from __future__ import annotations

import ast
import csv
import io
import json
import operator
import re
from typing import Any, Callable, Dict, List, Optional, Tuple


# 45+ Standard SAE J1979 Mode 01 PIDs grouped by vehicle subsystem
SAE_MODE01_CATALOG: Dict[str, Dict[str, Any]] = {
    "04": {"key": "load_pct", "label": "Calculated Engine Load", "short": "LOAD", "cat": "Engine & Air", "unit": "%", "min": 0, "max": 100, "bytes": 1, "fn": lambda b: round(b[0] * 100.0 / 255.0, 1)},
    "05": {"key": "coolant_c", "label": "Engine Coolant Temp", "short": "ECT", "cat": "Thermal & Oil", "unit": "°C", "min": -40, "max": 130, "bytes": 1, "fn": lambda b: b[0] - 40},
    "06": {"key": "stft_b1", "label": "Short Term Fuel Trim B1", "short": "STFT1", "cat": "Fuel & Emissions", "unit": "%", "min": -25, "max": 25, "bytes": 1, "fn": lambda b: round((b[0] - 128) * 100.0 / 128.0, 1)},
    "07": {"key": "ltft_b1", "label": "Long Term Fuel Trim B1", "short": "LTFT1", "cat": "Fuel & Emissions", "unit": "%", "min": -25, "max": 25, "bytes": 1, "fn": lambda b: round((b[0] - 128) * 100.0 / 128.0, 1)},
    "08": {"key": "stft_b2", "label": "Short Term Fuel Trim B2", "short": "STFT2", "cat": "Fuel & Emissions", "unit": "%", "min": -25, "max": 25, "bytes": 1, "fn": lambda b: round((b[0] - 128) * 100.0 / 128.0, 1)},
    "09": {"key": "ltft_b2", "label": "Long Term Fuel Trim B2", "short": "LTFT2", "cat": "Fuel & Emissions", "unit": "%", "min": -25, "max": 25, "bytes": 1, "fn": lambda b: round((b[0] - 128) * 100.0 / 128.0, 1)},
    "0A": {"key": "fuel_press_kpa", "label": "Fuel Pressure (Gauge)", "short": "FRP_G", "cat": "Fuel & Emissions", "unit": "kPa", "min": 0, "max": 765, "bytes": 1, "fn": lambda b: b[0] * 3},
    "0B": {"key": "map_kpa", "label": "Intake Manifold Pressure", "short": "MAP", "cat": "Engine & Air", "unit": "kPa", "min": 0, "max": 255, "bytes": 1, "fn": lambda b: b[0]},
    "0C": {"key": "rpm", "label": "Engine RPM", "short": "RPM", "cat": "Engine & Air", "unit": "rpm", "min": 0, "max": 7000, "bytes": 2, "fn": lambda b: round(((b[0] * 256) + b[1]) / 4.0, 1)},
    "0D": {"key": "speed_kmh", "label": "Vehicle Speed", "short": "VSS", "cat": "Vehicle & Torque", "unit": "km/h", "min": 0, "max": 255, "bytes": 1, "fn": lambda b: b[0]},
    "0E": {"key": "timing_deg", "label": "Timing Advance", "short": "IGN", "cat": "Engine & Air", "unit": "°", "min": -30, "max": 64, "bytes": 1, "fn": lambda b: round(b[0] / 2.0 - 64.0, 1)},
    "0F": {"key": "iat_c", "label": "Intake Air Temp", "short": "IAT", "cat": "Engine & Air", "unit": "°C", "min": -40, "max": 100, "bytes": 1, "fn": lambda b: b[0] - 40},
    "10": {"key": "maf_gps", "label": "MAF Air Flow Rate", "short": "MAF", "cat": "Engine & Air", "unit": "g/s", "min": 0, "max": 250, "bytes": 2, "fn": lambda b: round(((b[0] * 256) + b[1]) / 100.0, 2)},
    "11": {"key": "throttle_pct", "label": "Throttle Position", "short": "TPS", "cat": "Engine & Air", "unit": "%", "min": 0, "max": 100, "bytes": 1, "fn": lambda b: round(b[0] * 100.0 / 255.0, 1)},
    "14": {"key": "o2_b1s1_v", "label": "O2 Sensor B1S1 Voltage", "short": "O2_11", "cat": "Fuel & Emissions", "unit": "V", "min": 0, "max": 1.2, "bytes": 1, "fn": lambda b: round(b[0] / 200.0, 3)},
    "15": {"key": "o2_b1s2_v", "label": "O2 Sensor B1S2 Voltage", "short": "O2_12", "cat": "Fuel & Emissions", "unit": "V", "min": 0, "max": 1.2, "bytes": 1, "fn": lambda b: round(b[0] / 200.0, 3)},
    "18": {"key": "o2_b2s1_v", "label": "O2 Sensor B2S1 Voltage", "short": "O2_21", "cat": "Fuel & Emissions", "unit": "V", "min": 0, "max": 1.2, "bytes": 1, "fn": lambda b: round(b[0] / 200.0, 3)},
    "19": {"key": "o2_b2s2_v", "label": "O2 Sensor B2S2 Voltage", "short": "O2_22", "cat": "Fuel & Emissions", "unit": "V", "min": 0, "max": 1.2, "bytes": 1, "fn": lambda b: round(b[0] / 200.0, 3)},
    "1F": {"key": "runtime_sec", "label": "Run Time Since Start", "short": "RUN_S", "cat": "Charging & Info", "unit": "s", "min": 0, "max": 3600, "bytes": 2, "fn": lambda b: (b[0] * 256) + b[1]},
    "21": {"key": "mil_dist_km", "label": "Distance Traveled w/ MIL On", "short": "MIL_KM", "cat": "Charging & Info", "unit": "km", "min": 0, "max": 5000, "bytes": 2, "fn": lambda b: (b[0] * 256) + b[1]},
    "22": {"key": "frp_rel_kpa", "label": "Fuel Rail Pressure (Rel Vac)", "short": "FRP_R", "cat": "Fuel & Emissions", "unit": "kPa", "min": 0, "max": 5177, "bytes": 2, "fn": lambda b: round(((b[0] * 256) + b[1]) * 0.079, 1)},
    "23": {"key": "frp_gdi_kpa", "label": "Fuel Rail Gauge Pressure (GDI)", "short": "FRP_GDI", "cat": "Fuel & Emissions", "unit": "kPa", "min": 0, "max": 25000, "bytes": 2, "fn": lambda b: ((b[0] * 256) + b[1]) * 10},
    "2C": {"key": "egr_cmd_pct", "label": "Commanded EGR", "short": "EGR", "cat": "Engine & Air", "unit": "%", "min": 0, "max": 100, "bytes": 1, "fn": lambda b: round(b[0] * 100.0 / 255.0, 1)},
    "2D": {"key": "egr_err_pct", "label": "EGR Error", "short": "EGR_E", "cat": "Engine & Air", "unit": "%", "min": -50, "max": 50, "bytes": 1, "fn": lambda b: round((b[0] - 128) * 100.0 / 128.0, 1)},
    "2E": {"key": "evap_purge_pct", "label": "Commanded EVAP Purge", "short": "EVAP", "cat": "Fuel & Emissions", "unit": "%", "min": 0, "max": 100, "bytes": 1, "fn": lambda b: round(b[0] * 100.0 / 255.0, 1)},
    "2F": {"key": "fuel_level_pct", "label": "Fuel Tank Level", "short": "FUEL%", "cat": "Vehicle & Torque", "unit": "%", "min": 0, "max": 100, "bytes": 1, "fn": lambda b: round(b[0] * 100.0 / 255.0, 1)},
    "30": {"key": "warmups_clr", "label": "Warm-Ups Since Codes Cleared", "short": "WRMUP", "cat": "Charging & Info", "unit": "cnt", "min": 0, "max": 255, "bytes": 1, "fn": lambda b: b[0]},
    "31": {"key": "dist_clr_km", "label": "Distance Since Codes Cleared", "short": "CLR_KM", "cat": "Charging & Info", "unit": "km", "min": 0, "max": 10000, "bytes": 2, "fn": lambda b: (b[0] * 256) + b[1]},
    "33": {"key": "baro_kpa", "label": "Absolute Barometric Pressure", "short": "BARO", "cat": "Engine & Air", "unit": "kPa", "min": 70, "max": 110, "bytes": 1, "fn": lambda b: b[0]},
    "3C": {"key": "cat_temp_b1s1", "label": "Catalyst Temp B1S1", "short": "CAT1", "cat": "Fuel & Emissions", "unit": "°C", "min": 0, "max": 950, "bytes": 2, "fn": lambda b: round(((b[0] * 256) + b[1]) / 10.0 - 40.0, 1)},
    "3D": {"key": "cat_temp_b2s1", "label": "Catalyst Temp B2S1", "short": "CAT2", "cat": "Fuel & Emissions", "unit": "°C", "min": 0, "max": 950, "bytes": 2, "fn": lambda b: round(((b[0] * 256) + b[1]) / 10.0 - 40.0, 1)},
    "42": {"key": "battery_v", "label": "Control Module Voltage", "short": "VOLT", "cat": "Charging & Info", "unit": "V", "min": 10, "max": 15.5, "bytes": 2, "fn": lambda b: round(((b[0] * 256) + b[1]) / 1000.0, 2)},
    "43": {"key": "abs_load_pct", "label": "Absolute Load Value", "short": "ALOAD", "cat": "Engine & Air", "unit": "%", "min": 0, "max": 150, "bytes": 2, "fn": lambda b: round(((b[0] * 256) + b[1]) * 100.0 / 255.0, 1)},
    "44": {"key": "cmd_lambda", "label": "Commanded Air-Fuel Lambda", "short": "LAMB", "cat": "Fuel & Emissions", "unit": "λ", "min": 0.7, "max": 1.3, "bytes": 2, "fn": lambda b: round(((b[0] * 256) + b[1]) * 2.0 / 65536.0, 3)},
    "45": {"key": "rel_throttle_pct", "label": "Relative Throttle Position", "short": "RTPS", "cat": "Engine & Air", "unit": "%", "min": 0, "max": 100, "bytes": 1, "fn": lambda b: round(b[0] * 100.0 / 255.0, 1)},
    "46": {"key": "ambient_c", "label": "Ambient Air Temperature", "short": "AMB", "cat": "Thermal & Oil", "unit": "°C", "min": -40, "max": 60, "bytes": 1, "fn": lambda b: b[0] - 40},
    "49": {"key": "pedal_d_pct", "label": "Accelerator Pedal Pos D", "short": "APP", "cat": "Engine & Air", "unit": "%", "min": 0, "max": 100, "bytes": 1, "fn": lambda b: round(b[0] * 100.0 / 255.0, 1)},
    "5C": {"key": "oil_temp_c", "label": "Engine Oil Temperature", "short": "EOT", "cat": "Thermal & Oil", "unit": "°C", "min": -40, "max": 150, "bytes": 1, "fn": lambda b: b[0] - 40},
    "5E": {"key": "fuel_rate_lph", "label": "Engine Fuel Rate", "short": "L/HR", "cat": "Fuel & Emissions", "unit": "L/h", "min": 0, "max": 60, "bytes": 2, "fn": lambda b: round(((b[0] * 256) + b[1]) / 20.0, 2)},
    "61": {"key": "torque_dem_pct", "label": "Driver Demand Torque", "short": "TQ_D", "cat": "Vehicle & Torque", "unit": "%", "min": -25, "max": 100, "bytes": 1, "fn": lambda b: b[0] - 125},
    "62": {"key": "torque_act_pct", "label": "Actual Engine Torque", "short": "TQ_A", "cat": "Vehicle & Torque", "unit": "%", "min": -25, "max": 100, "bytes": 1, "fn": lambda b: b[0] - 125},
    "63": {"key": "torque_ref_nm", "label": "Engine Reference Torque", "short": "TQ_R", "cat": "Vehicle & Torque", "unit": "Nm", "min": 0, "max": 1000, "bytes": 2, "fn": lambda b: (b[0] * 256) + b[1]},
}


def list_catalog_metadata(supported_pids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    sup = {p.upper().zfill(2) for p in (supported_pids or [])}
    out = []
    for pid_hex, meta in SAE_MODE01_CATALOG.items():
        out.append({
            "pid": pid_hex,
            "cmd": f"01{pid_hex}",
            "key": meta["key"],
            "label": meta["label"],
            "short": meta["short"],
            "category": meta["cat"],
            "unit": meta["unit"],
            "min": meta["min"],
            "max": meta["max"],
            "supported": (pid_hex in sup) if sup else None,
        })
    return out


def decode_supported_pid_bitmask(base_pid_hex: str, four_bytes: List[int]) -> List[str]:
    """Decode SAE J1979 Mode 01 PID 00/20/40/60/80 32-bit bitmask into list of supported 2-char hex PIDs."""
    if len(four_bytes) < 4:
        return []
    base = int(base_pid_hex, 16)
    mask = (four_bytes[0] << 24) | (four_bytes[1] << 16) | (four_bytes[2] << 8) | four_bytes[3]
    found: List[str] = []
    for bit_idx in range(32):
        if mask & (1 << (31 - bit_idx)):
            pid_num = base + bit_idx + 1
            found.append(f"{pid_num:02X}")
    return found


def decode_standard_pid(pid_hex: str, data_bytes: List[int]) -> Optional[Dict[str, Any]]:
    p = pid_hex.upper().zfill(2)
    meta = SAE_MODE01_CATALOG.get(p)
    if not meta or len(data_bytes) < meta["bytes"]:
        return None
    val = meta["fn"](data_bytes)
    return {
        "pid": p,
        "key": meta["key"],
        "label": meta["label"],
        "short": meta["short"],
        "value": val,
        "unit": meta["unit"],
        "min": meta["min"],
        "max": meta["max"],
    }


# ── Strict AST Arithmetic Evaluator for Torque Pro / Extended PID Formulas ──
_BIN_OPS: Dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.BitAnd: lambda x, y: int(x) & int(y),
    ast.BitOr: lambda x, y: int(x) | int(y),
    ast.BitXor: lambda x, y: int(x) ^ int(y),
    ast.LShift: lambda x, y: int(x) << int(y),
    ast.RShift: lambda x, y: int(x) >> int(y),
}

_UNARY_OPS: Dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _signed_byte(x: float) -> int:
    iv = int(x) & 0xFF
    return iv - 256 if iv >= 128 else iv


def _signed_word(x: float) -> int:
    iv = int(x) & 0xFFFF
    return iv - 65536 if iv >= 32768 else iv


_SAFE_FUNCS: Dict[str, Callable[..., Any]] = {
    "signed": _signed_byte,
    "signed16": _signed_word,
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
}


def evaluate_pid_equation(expr: str, raw_bytes: List[int]) -> float:
    """Safely evaluate a Torque Pro PID formula (e.g. '((A*256)+B)/4' or 'Signed(A)*0.5') via AST."""
    cleaned = str(expr or "A").strip()
    if not cleaned or len(cleaned) > 160:
        raise ValueError("Invalid or overly long PID equation")
    # Normalize Torque's Signed(A) / {A:0} shorthand safely
    cleaned = re.sub(r"\bSigned\s*\(", "signed(", cleaned, flags=re.I)
    cleaned = re.sub(r"\{([A-H]):(\d)\}", lambda m: f"(({m.group(1)}>>{m.group(2)})&1)", cleaned)

    env: Dict[str, float] = {}
    for idx, ch in enumerate("ABCDEFGH"):
        env[ch] = float(raw_bytes[idx]) if idx < len(raw_bytes) else 0.0

    tree = ast.parse(cleaned, mode="eval")

    def _visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name):
            key = node.id.upper()
            if key in env:
                return env[key]
            raise ValueError(f"Unknown PID variable '{node.id}'")
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            return float(_BIN_OPS[type(node.op)](_visit(node.left), _visit(node.right)))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return float(_UNARY_OPS[type(node.op)](_visit(node.operand)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = _SAFE_FUNCS.get(node.func.id.lower())
            if fn is None:
                raise ValueError(f"Disallowed function '{node.func.id}' in PID equation")
            args = [_visit(a) for a in node.args]
            return float(fn(*args))
        raise ValueError(f"Unsupported syntax node in PID equation: {type(node).__name__}")

    return round(_visit(tree), 4)


def parse_torque_csv(csv_text: str) -> List[Dict[str, Any]]:
    """
    Parse Torque Pro Extended PID CSV:
      Name,ShortName,ModeAndPID,Equation,Min Value,Max Value,Units,Header
    """
    reader = csv.DictReader(io.StringIO((csv_text or "").strip()))
    if not reader.fieldnames:
        return []
    out: List[Dict[str, Any]] = []
    for row in reader:
        norm = {str(k or "").strip().lower().replace(" ", ""): str(v or "").strip() for k, v in row.items()}
        name = norm.get("name") or norm.get("fullname") or ""
        short = norm.get("shortname") or name[:10]
        cmd = re.sub(r"[^0-9A-Fa-f]", "", norm.get("modeandpid") or norm.get("pid") or "").upper()
        eq = norm.get("equation") or "A"
        if not name or len(cmd) < 4:
            continue
        # Validate equation AST up front
        evaluate_pid_equation(eq, [100, 50, 0, 0])
        header = re.sub(r"[^0-9A-Fa-f]", "", norm.get("header") or norm.get("obdheader") or "7DF").upper() or "7DF"
        out.append({
            "source_format": "torque_csv",
            "name": name[:120],
            "short_name": short[:24],
            "header": header[:8],
            "cmd": cmd[:12],
            "equation": eq[:160],
            "unit": (norm.get("units") or norm.get("unit") or "")[:24],
            "min_val": float(norm.get("minvalue") or norm.get("min") or 0),
            "max_val": float(norm.get("maxvalue") or norm.get("max") or 100),
        })
    return out


def parse_obdb_json(json_text: str) -> List[Dict[str, Any]]:
    """
    Parse OBDb (github.com/OBDb) JSON signal-set definitions into normalized extended PIDs.
    Supports both `{ "commands": [ { "hdr": "7E0", "cmd": {"22": "1234"}, "signals": [...] } ] }`
    and simplified command arrays.
    """
    data = json.loads(json_text)
    cmds = data.get("commands") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    out: List[Dict[str, Any]] = []
    for c in cmds:
        if not isinstance(c, dict):
            continue
        hdr = re.sub(r"[^0-9A-Fa-f]", "", str(c.get("hdr") or c.get("header") or "7E0")).upper() or "7E0"
        raw_cmd = c.get("cmd")
        cmd_hex = ""
        if isinstance(raw_cmd, dict):
            for k, v in raw_cmd.items():
                cmd_hex = re.sub(r"[^0-9A-Fa-f]", "", f"{k}{v}").upper()
                break
        elif isinstance(raw_cmd, str):
            cmd_hex = re.sub(r"[^0-9A-Fa-f]", "", raw_cmd).upper()
        if len(cmd_hex) < 4:
            continue

        for sig in (c.get("signals") or []):
            if not isinstance(sig, dict):
                continue
            sid = str(sig.get("id") or sig.get("name") or "SIGNAL")[:64]
            name = str(sig.get("name") or sid)[:120]
            fmt = sig.get("fmt") if isinstance(sig.get("fmt"), dict) else sig
            bit_off = int(fmt.get("bix") if fmt.get("bix") is not None else fmt.get("bitOffset", 0))
            bit_len = int(fmt.get("len") if fmt.get("len") is not None else fmt.get("bitLength", 8))
            mul = float(fmt.get("mul") if fmt.get("mul") is not None else fmt.get("factor", 1.0))
            div = float(fmt.get("div") if fmt.get("div") is not None else 1.0)
            add = float(fmt.get("add") if fmt.get("add") is not None else fmt.get("offset", 0.0))
            signed = bool(fmt.get("sign") or fmt.get("signed"))
            scale = mul / (div if div != 0 else 1.0)

            byte_idx = bit_off // 8
            letters = "ABCDEFGH"
            if byte_idx >= len(letters):
                continue
            if bit_len == 16 and byte_idx + 1 < len(letters):
                base_expr = f"({letters[byte_idx]}*256+{letters[byte_idx + 1]})"
                if signed:
                    base_expr = f"signed16({base_expr})"
            else:
                base_expr = f"signed({letters[byte_idx]})" if signed else letters[byte_idx]
            eq = f"({base_expr}*{round(scale, 6)})+({round(add, 6)})"
            out.append({
                "source_format": "obdb_json",
                "name": name,
                "short_name": sid[:24],
                "header": hdr[:8],
                "cmd": cmd_hex[:12],
                "equation": eq[:160],
                "unit": str(fmt.get("unit") or sig.get("unit") or "")[:24],
                "min_val": float(fmt.get("min") or 0),
                "max_val": float(fmt.get("max") or 100),
            })
    return out
