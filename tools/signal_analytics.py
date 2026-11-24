#!/usr/bin/env python3
"""
tools/signal_analytics.py — Automotive Time-Series Signal, Correlation & CAN-Bus Analysis Engine.

Computes descriptive/drift statistics, charging/voltage ripple health, cross-channel Pearson
correlations, Z-score transient anomalies, O2 sensor switching / catalyst storage ratios,
deterministic mechanical/electrical fault signatures, and CAN-bus frame jitter / dropout /
byte-mutability heatmaps for both human mechanics and LLM ReAct diagnosis.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as np  # type: ignore
except Exception:
    np = None  # Pure-Python math fallback

CHANNELS = [
    ("rpm", "Engine RPM", "rpm"),
    ("speed_kmh", "Vehicle Speed", "km/h"),
    ("coolant_c", "Coolant Temp", "°C"),
    ("load_pct", "Engine Load", "%"),
    ("throttle_pct", "Throttle Pos", "%"),
    ("maf_gps", "MAF Air Flow", "g/s"),
    ("map_kpa", "Intake MAP", "kPa"),
    ("stft_b1", "STFT Bank 1", "%"),
    ("ltft_b1", "LTFT Bank 1", "%"),
    ("stft_b2", "STFT Bank 2", "%"),
    ("ltft_b2", "LTFT Bank 2", "%"),
    ("timing_deg", "Timing Advance", "°"),
    ("iat_c", "Intake Air Temp", "°C"),
    ("battery_v", "Battery / ECU Voltage", "V"),
    ("o2_b1s1_v", "O2 Bank 1 Sensor 1", "V"),
    ("o2_b1s2_v", "O2 Bank 1 Sensor 2", "V"),
    ("knock_deg", "Knock Retard", "°"),
]


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 5:
        return None
    if np is not None:
        a = np.asarray(xs[:n], dtype=float)
        b = np.asarray(ys[:n], dtype=float)
        sa = float(np.std(a))
        sb = float(np.std(b))
        if sa < 1e-9 or sb < 1e-9:
            return None
        r = float(np.corrcoef(a, b)[0, 1])
        return round(r, 3) if not math.isnan(r) else None
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((xs[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ys[i] - my) ** 2 for i in range(n)))
    if dx < 1e-9 or dy < 1e-9:
        return None
    return round(num / (dx * dy), 3)


def _linear_slope_per_min(ts: List[float], vals: List[float]) -> float:
    n = min(len(ts), len(vals))
    if n < 3:
        return 0.0
    t0 = ts[0]
    xm = [(ts[i] - t0) / 60.0 for i in range(n)]
    span = xm[-1] - xm[0]
    if span < 1e-4:
        return 0.0
    mx = sum(xm) / n
    my = sum(vals[:n]) / n
    den = sum((x - mx) ** 2 for x in xm)
    if den < 1e-9:
        return 0.0
    num = sum((xm[i] - mx) * (vals[i] - my) for i in range(n))
    return round(num / den, 4)


def _percentile(sorted_vals: List[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def analyze_telemetry_run(
    samples: List[Dict[str, Any]],
    asset_meta: Optional[Dict[str, Any]] = None,
    historical_dtcs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run full time-series signal, voltage, O2, correlation, and anomaly analysis on recorded samples."""
    if not samples:
        return {
            "status": "empty",
            "sample_count": 0,
            "duration_sec": 0.0,
            "channels": {},
            "correlations": [],
            "voltage_health": {},
            "o2_catalyst_health": {},
            "signatures": [],
            "anomalies": [],
            "active_dtcs": [],
            "historical_dtcs": historical_dtcs or [],
            "asset": asset_meta or {},
        }

    ts_list = [float(s.get("ts") or i) for i, s in enumerate(samples)]
    t0 = ts_list[0]
    duration_sec = round(max(0.0, ts_list[-1] - t0), 2)

    series: Dict[str, List[float]] = {}
    channel_stats: Dict[str, Dict[str, Any]] = {}
    anomalies: List[Dict[str, Any]] = []

    # Build dynamic channel list: 17 core channels + every rich extra_pid / custom PID recorded in the 600-sample buffer
    all_channels: List[Tuple[str, str, str]] = list(CHANNELS)
    known_keys = {k for k, _, _ in CHANNELS}
    extra_meta: Dict[str, Tuple[str, str]] = {}
    try:
        import pid_catalog as _pc
        for _meta in _pc.SAE_MODE01_CATALOG.values():
            extra_meta[_meta["key"]] = (_meta["label"], _meta["unit"])
    except Exception:
        pass
    seen_extra: List[str] = []
    for s in samples:
        ep = s.get("extra_pids")
        if isinstance(ep, dict):
            for ek in ep.keys():
                if ek not in known_keys and ek not in seen_extra:
                    seen_extra.append(ek)
    for ek in sorted(seen_extra):
        lbl, unit = extra_meta.get(ek, (ek.replace("_", " ").title(), ""))
        all_channels.append((ek, lbl, unit))

    for key, label, unit in all_channels:
        vals: List[float] = []
        v_ts: List[float] = []
        for i, s in enumerate(samples):
            raw = s.get(key)
            if raw is None and isinstance(s.get("extra_pids"), dict):
                raw = s["extra_pids"].get(key)
            if raw is not None and raw != "":
                try:
                    vals.append(float(raw))
                    v_ts.append(ts_list[i])
                except (TypeError, ValueError):
                    pass
        if not vals:
            continue
        series[key] = vals
        sv = sorted(vals)
        mean = sum(vals) / len(vals)
        std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        pk_to_pk = sv[-1] - sv[0]
        slope = _linear_slope_per_min(v_ts, vals)
        channel_stats[key] = {
            "label": label,
            "unit": unit,
            "count": len(vals),
            "mean": round(mean, 3),
            "std": round(std, 3),
            "min": round(sv[0], 3),
            "max": round(sv[-1], 3),
            "p5": round(_percentile(sv, 5), 3),
            "p95": round(_percentile(sv, 95), 3),
            "pk_to_pk": round(pk_to_pk, 3),
            "drift_per_min": slope,
            "last": round(vals[-1], 3),
        }
        # Z-score transient glitch detector (|z| >= 2.6)
        if std > 1e-6 and len(vals) >= 8:
            for idx, v in enumerate(vals):
                z = (v - mean) / std
                if abs(z) >= 2.6:
                    anomalies.append({
                        "channel": key,
                        "label": label,
                        "rel_sec": round(v_ts[idx] - t0, 1),
                        "value": round(v, 3),
                        "unit": unit,
                        "z_score": round(z, 2),
                        "direction": "SPIKE" if z > 0 else "DROP",
                    })

    # Derived total Bank 1 fuel trim series (STFT + LTFT)
    if "stft_b1" in series and "ltft_b1" in series:
        n_ft = min(len(series["stft_b1"]), len(series["ltft_b1"]))
        series["total_ft_b1"] = [series["stft_b1"][i] + series["ltft_b1"][i] for i in range(n_ft)]
    if "stft_b2" in series and "ltft_b2" in series:
        n_ft2 = min(len(series["stft_b2"]), len(series["ltft_b2"]))
        series["total_ft_b2"] = [series["stft_b2"][i] + series["ltft_b2"][i] for i in range(n_ft2)]

    # Diagnostic cross-channel correlation pairs (de-biased: always highlight confounders to falsify!)
    diag_pairs: List[Tuple[str, str, str]] = [
        ("total_ft_b1", "rpm", "Total Fuel Trim B1 vs RPM (negative r often points to idle vacuum leak; positive r can mean EITHER fuel delivery drop OR contaminated/under-reporting MAF OR post-MAF/turbo charge-pipe leak — MUST cross-check fuel_press_kpa & frp_gdi_kpa before concluding fuel starvation)"),
        ("total_ft_b1", "load_pct", "Total Fuel Trim B1 vs Engine Load (distinguishes high-vacuum light-load free-revving from true high-load starvation)"),
        ("total_ft_b1", "maf_gps", "Total Fuel Trim B1 vs MAF Air Flow (positive r with stable fuel rail pressure strongly favors dirty/under-reporting MAF or post-MAF intake/charge-pipe leak)"),
        ("o2_b1s1_v", "o2_b1s2_v", "Upstream O2 B1S1 vs Downstream O2 B1S2 (high r = depleted catalyst storage)"),
        ("battery_v", "rpm", "Battery Voltage vs RPM (charging regulator coupling)"),
        ("battery_v", "load_pct", "Battery Voltage vs Engine Load (voltage sag under load)"),
        ("maf_gps", "rpm", "MAF Air Flow vs RPM (volumetric breathing coherence)"),
        ("map_kpa", "throttle_pct", "Intake MAP vs Throttle Position (boost/vacuum response)"),
        ("timing_deg", "knock_deg", "Timing Advance vs Knock Retard"),
        ("coolant_c", "rpm", "Coolant Temp vs RPM"),
    ]
    correlations: List[Dict[str, Any]] = []
    corr_map: Dict[Tuple[str, str], float] = {}
    for c1, c2, desc in diag_pairs:
        if c1 in series and c2 in series:
            r = _pearson(series[c1], series[c2])
            if r is not None:
                corr_map[(c1, c2)] = r
                correlations.append({
                    "pair": f"{c1} ↔ {c2}",
                    "a": c1,
                    "b": c2,
                    "r": r,
                    "strength": "STRONG" if abs(r) >= 0.7 else ("MODERATE" if abs(r) >= 0.4 else "WEAK"),
                    "interpretation": desc,
                })
    correlations.sort(key=lambda x: -abs(x["r"]))

    # Collect active DTCs seen across the run
    active_dtcs: List[str] = []
    seen_dtc = set()
    for s in samples:
        for d in (s.get("dtcs") or []):
            code = d.get("code") if isinstance(d, dict) else str(d)
            if code and code not in seen_dtc:
                seen_dtc.add(code)
                active_dtcs.append(code)

    # Voltage & Charging Health Analysis
    v_stat = channel_stats.get("battery_v")
    rpm_stat = channel_stats.get("rpm")
    voltage_health: Dict[str, Any] = {"status": "NO_DATA"}
    if v_stat:
        running = (rpm_stat is not None and rpm_stat["mean"] > 400)
        v_min = v_stat["min"]
        v_max = v_stat["max"]
        v_pp = v_stat["pk_to_pk"]
        v_std = v_stat["std"]
        flags: List[str] = []
        if running and v_min < 12.2:
            flags.append(f"UNDER_VOLTAGE_RUNNING (min {v_min}V < 12.2V)")
        elif not running and v_min < 11.8:
            flags.append(f"WEAK_BATTERY_KOEO (min {v_min}V < 11.8V)")
        if v_max > 14.9:
            flags.append(f"OVER_VOLTAGE_REGULATOR (max {v_max}V > 14.9V)")
        if running and (v_pp >= 0.45 or v_std >= 0.16):
            flags.append(f"HIGH_VOLTAGE_RIPPLE_OR_SAG (pk-pk {v_pp}V, std {v_std}V)")
        voltage_health = {
            "status": "FAULT" if flags else "HEALTHY",
            "engine_running": running,
            "mean_v": v_stat["mean"],
            "min_v": v_min,
            "max_v": v_max,
            "ripple_vpp": v_pp,
            "std_v": v_std,
            "flags": flags,
        }

    # O2 Switching & Catalyst Storage Health
    o2_up = channel_stats.get("o2_b1s1_v")
    o2_dn = channel_stats.get("o2_b1s2_v")
    o2_health: Dict[str, Any] = {"status": "NO_DATA"}
    if o2_up and o2_dn:
        amp_ratio = round(o2_dn["pk_to_pk"] / max(0.05, o2_up["pk_to_pk"]), 2)
        o2_r = corr_map.get(("o2_b1s1_v", "o2_b1s2_v"))
        cat_status = "HEALTHY"
        note = "Upstream O2 switching cleanly; downstream O2 steady (good catalyst O2 storage)."
        if o2_dn["pk_to_pk"] > 0.35 and (amp_ratio >= 0.55 or (o2_r is not None and o2_r >= 0.55)):
            cat_status = "CATALYST_DEGRADED"
            note = f"Downstream O2 B1S2 mirrors upstream switching (amp ratio {amp_ratio}, r={o2_r}) — classic depleted catalytic converter signature."
        elif o2_up["max"] < 0.30 and o2_up["pk_to_pk"] < 0.18:
            cat_status = "BIASED_LEAN"
            note = f"Upstream O2 B1S1 stuck/biased lean (mean {o2_up['mean']}V, max {o2_up['max']}V)."
        o2_health = {
            "status": cat_status,
            "b1s1_vpp": o2_up["pk_to_pk"],
            "b1s2_vpp": o2_dn["pk_to_pk"],
            "downstream_to_upstream_ratio": amp_ratio,
            "o2_correlation_r": o2_r,
            "note": note,
        }

    # Automated Mechanical & Electrical Diagnostic Signatures (with orthogonal falsification checks!)
    signatures: List[Dict[str, Any]] = []
    if "total_ft_b1" in series:
        ft_mean = sum(series["total_ft_b1"]) / len(series["total_ft_b1"])
        ft_max = max(series["total_ft_b1"])
        r_rpm = corr_map.get(("total_ft_b1", "rpm"))
        r_load = corr_map.get(("total_ft_b1", "load_pct"))
        lp_stat = channel_stats.get("fuel_press_kpa")
        gdi_stat = channel_stats.get("frp_gdi_kpa")
        # Determine whether observed fuel pressure stayed healthy/stable (falsifying simple lift-pump/filter starvation!)
        fuel_press_stable = False
        fp_evidence_parts: List[str] = []
        if lp_stat and lp_stat["min"] >= 300.0 and lp_stat["pk_to_pk"] <= max(60.0, lp_stat["mean"] * 0.18):
            fuel_press_stable = True
            fp_evidence_parts.append(f"low-side fuel_press_kpa steady {lp_stat['min']}–{lp_stat['max']} kPa")
        if gdi_stat and gdi_stat["min"] >= 1800.0 and gdi_stat["max"] >= gdi_stat["mean"]:
            fuel_press_stable = True
            fp_evidence_parts.append(f"GDI rail frp_gdi_kpa scaled {gdi_stat['min']}–{gdi_stat['max']} kPa")

        if ft_mean > 10.0 or ft_max > 15.0:
            if r_rpm is not None and r_rpm <= -0.45:
                signatures.append({
                    "code": "VACUUM_LEAK_SIGNATURE",
                    "severity": "high",
                    "confidence": "HIGH" if r_rpm <= -0.65 else "MEDIUM",
                    "evidence": f"Bank 1 total fuel trim avg {ft_mean:+.1f}% (peak {ft_max:+.1f}%) drops as RPM increases (r={r_rpm}). Strong unmetered intake/PCV vacuum leak signature.",
                    "recommended_checks": ["Smoke-test intake boot, PCV valve/hose, brake booster line, and intake manifold gaskets", "Inspect idle MAP and MAF coherence"],
                })
            elif ((r_load is not None and r_load >= 0.45) or (r_rpm is not None and r_rpm >= 0.55)) and fuel_press_stable:
                signatures.append({
                    "code": "HIGH_RPM_LEAN_TRIM_WITH_STABLE_FUEL_PRESSURE",
                    "severity": "medium",
                    "confidence": "MEDIUM",
                    "evidence": (
                        f"Bank 1 total fuel trim avg {ft_mean:+.1f}% (peak {ft_max:+.1f}%) climbed with RPM (r={r_rpm}), "
                        f"HOWEVER fuel pressure remained stable ({'; '.join(fp_evidence_parts)}). "
                        "This falsifies simple low-side pump/filter starvation and points instead to an under-reporting/contaminated MAF sensor, "
                        "post-MAF / turbo charge-pipe or PCV leak, fuel ethanol blend, or stationary free-revving test artifacts."
                    ),
                    "recommended_checks": [
                        "Inspect/clean MAF element and compare measured MAF g/s against calculated volumetric airflow at steady RPM",
                        "Inspect post-MAF intake duct, turbo charge pipes/intercooler boots, and PCV check valve for leaks",
                        "Verify fuel trims during steady road-load cruise vs stationary revving and check ambient/winter ethanol fuel blend",
                    ],
                })
            elif r_load is not None and r_load >= 0.45:
                signatures.append({
                    "code": "FUEL_STARVATION_UNDER_LOAD",
                    "severity": "high",
                    "confidence": "MEDIUM",
                    "evidence": f"Bank 1 total fuel trim avg {ft_mean:+.1f}% worsens under engine load (r={r_load}) and fuel rail pressure channels were not confirmed stable. Could be fuel delivery restriction OR under-reporting MAF.",
                    "recommended_checks": ["Log low-pressure & GDI fuel rail pressure (PIDs 010A/0123) under real road load before replacing fuel components", "Compare MAF readings vs engine displacement/RPM"],
                })
            else:
                signatures.append({
                    "code": "ELEVATED_LEAN_FUEL_TRIM",
                    "severity": "medium",
                    "confidence": "MEDIUM",
                    "evidence": f"Bank 1 total fuel trim avg {ft_mean:+.1f}% (peak {ft_max:+.1f}%) exceeds ±10% normal corridor (check operating-regime snapshots and fuel pressure before concluding cause).",
                    "recommended_checks": ["Compare idle vs steady-load trims alongside MAF, MAP, and fuel pressure snapshots"],
                })

    if voltage_health.get("status") == "FAULT":
        has_u_codes = any(c.startswith("U") for c in active_dtcs)
        signatures.append({
            "code": "CHARGING_VOLTAGE_FAULT_WITH_CAN_IMPACT" if has_u_codes else "CHARGING_OR_VOLTAGE_INSTABILITY",
            "severity": "critical" if has_u_codes else "high",
            "confidence": "HIGH",
            "evidence": "; ".join(voltage_health["flags"]) + (f" coinciding with CAN U-codes ({', '.join([c for c in active_dtcs if c.startswith('U')])})." if has_u_codes else "."),
            "recommended_checks": [
                "Load-test 12V battery & clean chassis/engine ground straps before replacing any ECU",
                "Measure AC voltage across battery terminals at idle (expect < 50 mV AC; > 100 mV AC indicates failing alternator rectifier diode)",
            ],
        })

    if o2_health.get("status") == "CATALYST_DEGRADED":
        signatures.append({
            "code": "CATALYST_STORAGE_DEPLETED",
            "severity": "medium",
            "confidence": "HIGH",
            "evidence": o2_health["note"],
            "recommended_checks": ["Verify zero exhaust leaks between B1S1 and B1S2 before replacing catalytic converter", "Confirm prior misfire or oil-burn root cause is fixed first"],
        })

    c_stat = channel_stats.get("coolant_c")
    if c_stat and c_stat["max"] >= 106.0:
        signatures.append({
            "code": "COOLING_OVERTEMP_WARNING",
            "severity": "critical",
            "confidence": "HIGH",
            "evidence": f"Coolant peaked at {c_stat['max']}°C (drift {c_stat['drift_per_min']:+.2f}°C/min).",
            "recommended_checks": ["Check coolant level, radiator fan engagement, thermostat opening, and pressure-test cooling system"],
        })

    k_stat = channel_stats.get("knock_deg")
    if k_stat and k_stat["max"] >= 4.0:
        signatures.append({
            "code": "ELEVATED_KNOCK_RETARD",
            "severity": "medium",
            "confidence": "MEDIUM",
            "evidence": f"Knock retard peaked at {k_stat['max']}° (mean {k_stat['mean']}°).",
            "recommended_checks": ["Check fuel octane/quality, carbon buildup, spark plug heat range, and lean condition"],
        })

    # Operating-Regime Multi-Channel Snapshots (idle vs peak RPM vs peak load vs peak fuel trim vs min voltage/fuel pressure)
    # Gives the Copilot raw multi-sensor context at key physical moments instead of trusting summary correlations alone!
    def _snap(idx: int) -> Dict[str, Any]:
        s = samples[idx]
        ep = s.get("extra_pids") if isinstance(s.get("extra_pids"), dict) else {}
        row: Dict[str, Any] = {"rel_sec": round(ts_list[idx] - t0, 1)}
        for k in (
            "rpm", "speed_kmh", "load_pct", "throttle_pct", "map_kpa", "maf_gps",
            "stft_b1", "ltft_b1", "coolant_c", "iat_c", "timing_deg", "battery_v",
            "o2_b1s1_v", "o2_b1s2_v",
        ):
            if s.get(k) is not None:
                row[k] = s[k]
        if row.get("stft_b1") is not None and row.get("ltft_b1") is not None:
            row["total_ft_b1"] = round(float(row["stft_b1"]) + float(row["ltft_b1"]), 2)
        for ek in ("fuel_press_kpa", "frp_gdi_kpa", "cat_temp_b1s1", "cmd_lambda", "evap_purge_pct", "baro_kpa", "ambient_c", "abs_load_pct", "pedal_d_pct"):
            if ep.get(ek) is not None:
                row[ek] = ep[ek]
        return row

    regime_snapshots: Dict[str, Any] = {}
    rpm_idxs = [(i, float(samples[i]["rpm"])) for i in range(len(samples)) if samples[i].get("rpm") is not None]
    idle_cands = [(i, v) for i, v in rpm_idxs if 400.0 <= v <= 1050.0]
    if idle_cands:
        idle_cands.sort(key=lambda x: x[1])
        regime_snapshots["idle_baseline"] = _snap(idle_cands[len(idle_cands) // 2][0])
    if rpm_idxs:
        regime_snapshots["peak_rpm"] = _snap(max(rpm_idxs, key=lambda x: x[1])[0])
    load_idxs = [(i, float(samples[i]["map_kpa"] if samples[i].get("map_kpa") is not None else samples[i]["load_pct"])) for i in range(len(samples)) if samples[i].get("map_kpa") is not None or samples[i].get("load_pct") is not None]
    if load_idxs:
        regime_snapshots["peak_load_or_map"] = _snap(max(load_idxs, key=lambda x: x[1])[0])
    ft_idxs = [(i, abs(float(samples[i]["stft_b1"]) + float(samples[i]["ltft_b1"]))) for i in range(len(samples)) if samples[i].get("stft_b1") is not None and samples[i].get("ltft_b1") is not None]
    if ft_idxs:
        regime_snapshots["peak_fuel_trim"] = _snap(max(ft_idxs, key=lambda x: x[1])[0])
    bv_idxs = [(i, float(samples[i]["battery_v"])) for i in range(len(samples)) if samples[i].get("battery_v") is not None]
    if bv_idxs:
        regime_snapshots["min_battery_v"] = _snap(min(bv_idxs, key=lambda x: x[1])[0])
    fp_idxs = [
        (i, float((samples[i].get("extra_pids") or {}).get("fuel_press_kpa")))
        for i in range(len(samples))
        if isinstance(samples[i].get("extra_pids"), dict) and samples[i]["extra_pids"].get("fuel_press_kpa") is not None
    ]
    if fp_idxs:
        regime_snapshots["min_fuel_pressure"] = _snap(min(fp_idxs, key=lambda x: x[1])[0])

    # Compact plot series for frontend oscilloscope charts (max 150 points across 600-sample buffer)
    step = max(1, len(samples) // 150)
    plot_points = []
    for i in range(0, len(samples), step):
        s = samples[i]
        plot_points.append({
            "t": round(ts_list[i] - t0, 1),
            "rpm": s.get("rpm"),
            "load_pct": s.get("load_pct"),
            "coolant_c": s.get("coolant_c"),
            "battery_v": s.get("battery_v"),
            "stft_b1": s.get("stft_b1"),
            "ltft_b1": s.get("ltft_b1"),
            "stft_b2": s.get("stft_b2"),
            "ltft_b2": s.get("ltft_b2"),
            "maf_gps": s.get("maf_gps"),
            "o2_b1s1_v": s.get("o2_b1s1_v"),
            "o2_b1s2_v": s.get("o2_b1s2_v"),
            "timing_deg": s.get("timing_deg"),
            "knock_deg": s.get("knock_deg"),
        })

    return {
        "status": "ok",
        "sample_count": len(samples),
        "duration_sec": duration_sec,
        "buffer_window": {
            "buffered_samples": len(samples),
            "max_buffer_samples": 600,
            "channels_captured": len(channel_stats),
        },
        "asset": asset_meta or {},
        "active_dtcs": active_dtcs,
        "historical_dtcs": historical_dtcs or [],
        "regime_snapshots": regime_snapshots,
        "channels": channel_stats,
        "correlations": correlations,
        "voltage_health": voltage_health,
        "o2_catalyst_health": o2_health,
        "signatures": signatures,
        "anomalies": anomalies[:25],
        "plot_series": plot_points,
    }


def analyze_can_stream(frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Deep CAN-bus bus-health & reverse-engineering observer for raw frames:
    - Per-ID frame rate (Hz), mean/median period (ms), timing jitter (std ms)
    - Dropout gap detection (max_gap > 3.5x median period -> intermittent ECU / wiring fault)
    - Per-byte mutability heatmap (STATIC vs DYNAMIC vs COUNTER/TOGGLE across bytes 0..7)
    """
    if not frames:
        return {"status": "empty", "total_frames": 0, "bus_hz": 0.0, "id_profiles": [], "dropout_alerts": []}

    by_id: Dict[str, List[Dict[str, Any]]] = {}
    timestamps: List[float] = []
    for f in frames:
        cid = str(f.get("id") or "0x000")
        by_id.setdefault(cid, []).append(f)
        if f.get("ts") is not None:
            timestamps.append(float(f["ts"]))

    span_sec = (max(timestamps) - min(timestamps)) if len(timestamps) >= 2 else 0.0
    bus_hz = round(len(frames) / span_sec, 1) if span_sec > 0.01 else 0.0

    id_profiles: List[Dict[str, Any]] = []
    dropout_alerts: List[Dict[str, Any]] = []

    for cid, flist in sorted(by_id.items(), key=lambda kv: -len(kv[1])):
        dts: List[float] = []
        ts_vals = sorted([float(x["ts"]) for x in flist if x.get("ts") is not None])
        for i in range(1, len(ts_vals)):
            dts.append((ts_vals[i] - ts_vals[i - 1]) * 1000.0)

        med_ms = round(statistics.median(dts), 2) if dts else None
        std_ms = round(statistics.pstdev(dts), 2) if len(dts) > 1 else 0.0
        max_ms = round(max(dts), 2) if dts else None
        hz = round(1000.0 / med_ms, 1) if (med_ms and med_ms > 0.01) else None

        has_dropout = False
        if med_ms and max_ms and len(dts) >= 3 and max_ms > max(med_ms * 3.2, 80.0):
            has_dropout = True
            dropout_alerts.append({
                "arb_id": cid,
                "median_period_ms": med_ms,
                "max_gap_ms": max_ms,
                "note": f"CAN ID {cid} exhibited a {max_ms:.1f}ms dropout gap (normal {med_ms:.1f}ms) — inspect ECU power/ground or CAN-H/L splice.",
            })

        # Byte-level mutability heatmap across bytes 0..7
        max_dlc = max((int(x.get("dlc") or 0) for x in flist), default=0)
        byte_heatmap: List[Dict[str, Any]] = []
        for bidx in range(min(8, max_dlc)):
            seen_vals: List[int] = []
            for x in flist:
                dh = str(x.get("data") or "")
                if len(dh) >= (bidx + 1) * 2:
                    seen_vals.append(int(dh[bidx * 2 : bidx * 2 + 2], 16))
            uniq = sorted(set(seen_vals))
            if not uniq:
                continue
            kind = "STATIC" if len(uniq) == 1 else ("LOW_STATE" if len(uniq) <= 4 else "DYNAMIC")
            byte_heatmap.append({
                "byte": bidx,
                "kind": kind,
                "unique_count": len(uniq),
                "min_hex": f"0x{uniq[0]:02X}",
                "max_hex": f"0x{uniq[-1]:02X}",
            })

        id_profiles.append({
            "arb_id": cid,
            "count": len(flist),
            "dlc": max_dlc,
            "hz": hz,
            "median_period_ms": med_ms,
            "jitter_ms": std_ms,
            "max_gap_ms": max_ms,
            "dropout_detected": has_dropout,
            "byte_heatmap": byte_heatmap,
            "sample_data": flist[-1].get("data", ""),
        })

    return {
        "status": "ok",
        "total_frames": len(frames),
        "unique_id_count": len(by_id),
        "duration_sec": round(span_sec, 3),
        "bus_hz": bus_hz,
        "dropout_alerts": dropout_alerts,
        "id_profiles": id_profiles[:32],
    }
