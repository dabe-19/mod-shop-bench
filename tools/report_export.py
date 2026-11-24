#!/usr/bin/env python3
"""
tools/report_export.py — Diagnostic & LLM Mechanic Copilot Report Exporter for mod-shop-bench.

Bundles vehicle metadata, active & historical DTCs with plain-English safety guidance,
recorded time-series signal & correlation analytics, voltage-ripple & O2-catalyst health,
CAN-bus observer results, AI Mechanic Copilot triage logs & checklists, service intervals,
and torque specs into downloadable Markdown (.md), Print-Ready HTML (.html), or JSON (.json).

Security guarantees:
- All dynamic values rendered into HTML are strictly escaped with html.escape(..., quote=True).
- Exported files are persisted strictly inside MODULE_DATA_DIR/exports with sanitized names,
  canonical path containment verification, and 0600 permissions.
"""
from __future__ import annotations

import datetime
import html
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import obd_can
import shop_db
import signal_analytics

ALLOWED_FORMATS = ("markdown", "html", "json")
SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def exports_dir() -> Path:
    d = (shop_db.data_dir() / "exports").resolve()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _utc_iso(ts: Optional[float] = None) -> str:
    dt = datetime.datetime.fromtimestamp(ts if ts is not None else shop_db.now_ts(), tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _drive_safety_hint(sev: str) -> str:
    s = (sev or "medium").lower()
    if s == "critical":
        return "STOP / INSPECT SOON — risk of stalling, overheating, or loss of control."
    if s == "high":
        return "CAUTION — drive gently to shop; prolonged driving may damage catalyst or leave you stranded."
    return "MONITOR — generally safe for short trips; schedule diagnosis soon."


def collect_report_data(
    conn: sqlite3.Connection,
    asset_id: Optional[int] = None,
    run_id: Optional[int] = None,
) -> Dict[str, Any]:
    bundle = shop_db.get_asset_bundle(conn, asset_id=asset_id)
    asset = (bundle or {}).get("asset") or {}
    aid = asset.get("id")

    # Telemetry analysis (requested run_id or latest run for asset)
    selected_run_id = run_id
    if not selected_run_id:
        runs = shop_db.list_telemetry_runs(conn, asset_id=aid, limit=1)
        if runs:
            selected_run_id = runs[0]["id"]

    analysis: Optional[Dict[str, Any]] = None
    if selected_run_id:
        run_row, samples = shop_db.get_telemetry_run_samples(conn, int(selected_run_id))
        if run_row and samples:
            hist = shop_db.collect_historical_dtcs(conn, asset_id=aid)
            asset_meta = {
                "asset_id": aid,
                "name": asset.get("name", ""),
                "year": run_row.get("vehicle_year") or asset.get("year", ""),
                "make": run_row.get("vehicle_make") or asset.get("make", ""),
                "model": run_row.get("vehicle_model") or asset.get("model", ""),
                "vin": run_row.get("vin") or asset.get("vin_or_serial", ""),
                "engine_spec": asset.get("engine_spec", ""),
                "run_id": run_row["id"],
                "run_label": run_row.get("run_label", ""),
                "transport": run_row.get("transport", ""),
            }
            analysis = signal_analytics.analyze_telemetry_run(samples, asset_meta=asset_meta, historical_dtcs=hist)
            analysis["run"] = run_row

    # Collect active & historical DTCs with plain-English explanations
    active_codes: List[str] = []
    if analysis and analysis.get("active_dtcs"):
        active_codes.extend(str(c) for c in analysis["active_dtcs"])
    latest_sessions = (bundle or {}).get("obd_sessions") or []
    if latest_sessions:
        for d in latest_sessions[0].get("dtcs") or []:
            code = d.get("code") if isinstance(d, dict) else str(d)
            if code and code not in active_codes:
                active_codes.append(code)
    hist_codes = shop_db.collect_historical_dtcs(conn, asset_id=aid)
    dtc_details = []
    for code in active_codes:
        info = obd_can.lookup_dtc(code)
        info["driveability"] = _drive_safety_hint(info.get("severity", "medium"))
        dtc_details.append(info)

    # Latest CAN-bus capture analysis
    can_summary: Optional[Dict[str, Any]] = None
    can_captures = (bundle or {}).get("can_captures") or []
    if can_captures:
        latest_can = can_captures[0]
        raw_can = latest_can.get("raw_sample") or ""
        if raw_can:
            parsed = obd_can.parse_candump_text(raw_can)
            can_summary = {
                "capture_id": latest_can.get("id"),
                "interface": latest_can.get("interface"),
                "created_at": _utc_iso(latest_can.get("created_at")),
                "frame_count": parsed.get("frame_count", latest_can.get("frame_count", 0)),
                "unique_ids": parsed.get("unique_ids", []),
                "bus_analysis": parsed.get("bus_analysis", {}),
            }
        else:
            can_summary = {
                "capture_id": latest_can.get("id"),
                "interface": latest_can.get("interface"),
                "created_at": _utc_iso(latest_can.get("created_at")),
                "frame_count": latest_can.get("frame_count", 0),
                "unique_ids": latest_can.get("unique_ids", []),
                "summary": latest_can.get("summary", ""),
            }

    return {
        "generated_at": _utc_iso(),
        "generator": "Blue Lodge Shop Bench v2.6",
        "asset": asset,
        "active_dtcs": dtc_details,
        "historical_dtcs": hist_codes,
        "telemetry_analysis": analysis,
        "can_analysis": can_summary,
        "copilot_and_service_logs": (bundle or {}).get("logs") or [],
        "service_intervals": (bundle or {}).get("intervals") or [],
        "torque_specs": (bundle or {}).get("torque_specs") or [],
        "parts_inventory": (bundle or {}).get("parts") or [],
    }


def render_markdown(data: Dict[str, Any]) -> str:
    a = data.get("asset") or {}
    title = f"{a.get('year', '')} {a.get('make', '')} {a.get('model', '')} — {a.get('name', 'Vehicle / Equipment')}".strip(" —")
    lines: List[str] = [
        f"# Blue Lodge Shop Bench — Diagnostic & Copilot Report",
        "",
        f"- **Asset / Vehicle:** {title or 'Unassigned Fleet Asset'}",
        f"- **VIN / Serial:** `{a.get('vin_or_serial') or 'N/A'}`",
        f"- **Engine / Spec:** {a.get('engine_spec') or 'N/A'}",
        f"- **Odometer / Hours:** {a.get('meter_value', 0)} {a.get('meter_unit') or 'mi'}",
        f"- **Generated:** {data.get('generated_at')}",
        "",
    ]

    # 1. Active & Historical DTCs
    lines.append("## 1. Diagnostic Trouble Codes (DTCs)")
    dtcs = data.get("active_dtcs") or []
    if dtcs:
        for d in dtcs:
            lines.append(f"### `{d.get('code')}` — {d.get('title', 'Trouble Code')} ({str(d.get('severity', 'medium')).upper()})")
            lines.append(f"- **System:** {d.get('system', 'ECU')}")
            lines.append(f"- **Driveability:** {d.get('driveability', '')}")
            causes = d.get("causes") or []
            if causes:
                lines.append(f"- **Common Causes:** {'; '.join(causes)}")
            lines.append("")
    else:
        lines.append("_No active trouble codes recorded._")
        lines.append("")
    hist = data.get("historical_dtcs") or []
    lines.append(f"**Historical DTCs on file:** {', '.join(f'`{c}`' for c in hist) if hist else 'None'}")
    lines.append("")

    # 2. LLM Mechanic Copilot & Service Logs
    lines.append("## 2. AI Mechanic Copilot Diagnoses & Maintenance Logs")
    logs = data.get("copilot_and_service_logs") or []
    if logs:
        for lg in logs:
            lines.append(f"### [{str(lg.get('log_type', 'log')).upper()}] {lg.get('title', 'Untitled Log')} ({_utc_iso(lg.get('created_at'))})")
            if lg.get("summary"):
                lines.append("")
                lines.append(str(lg["summary"]).strip())
                lines.append("")
            chk = lg.get("checklist") or []
            if chk:
                lines.append("**Step-by-Step Checklist:**")
                for item in chk:
                    lines.append(f"- [ ] {item}")
                lines.append("")
    else:
        lines.append("_No LLM Mechanic Copilot triage or maintenance logs saved yet._")
        lines.append("")

    # 3. Signal & Correlation Analytics
    an = data.get("telemetry_analysis")
    lines.append("## 3. Time-Series Telemetry & Signal Correlation Lab")
    if an and an.get("status") == "ok":
        run = an.get("run") or {}
        lines.append(f"- **Run:** #{run.get('id', '—')} — {run.get('run_label', 'Telemetry Run')} (`{run.get('transport', 'sim')}`)")
        lines.append(f"- **Samples / Duration:** {an.get('sample_count', 0)} samples over {an.get('duration_sec', 0)}s")
        vh = an.get("voltage_health") or {}
        if vh:
            lines.append(f"- **Voltage & Ripple Health:** **{vh.get('status', '—')}** (Min {vh.get('min_v', '—')}V / Max {vh.get('max_v', '—')}V / Ripple {vh.get('ripple_vpp', '—')} Vpp) — {vh.get('assessment', '')}")
        o2 = an.get("o2_catalyst_health") or {}
        if o2:
            lines.append(f"- **O2 / Catalyst Health:** **{o2.get('status', '—')}** (Pearson r={o2.get('o2_correlation_r', '—')}, Ratio {o2.get('downstream_to_upstream_ratio', '—')}) — {o2.get('assessment', '')}")
        lines.append("")

        sigs = an.get("signatures") or []
        if sigs:
            lines.append("### Automated Fault Signatures")
            for sg in sigs:
                lines.append(f"- **{sg.get('code')}** ({sg.get('severity', 'warn').upper()} · {sg.get('confidence', '')} confidence): {sg.get('evidence', '')}")
                checks = sg.get("recommended_checks") or []
                if checks:
                    lines.append(f"  - Recommended shop checks: {'; '.join(checks)}")
            lines.append("")

        corrs = an.get("correlations") or []
        if corrs:
            lines.append("### Cross-Channel Pearson Correlations")
            lines.append("| Signal Pair | Pearson r | Interpretation |")
            lines.append("| :--- | :---: | :--- |")
            for c in corrs[:10]:
                lines.append(f"| {c.get('pair')} | `{c.get('r')}` | {c.get('interpretation')} |")
            lines.append("")

        ch_stats = an.get("channel_stats") or {}
        if ch_stats:
            lines.append("### Per-Channel Telemetry Statistics")
            lines.append("| Channel | Min | Mean | Max | StdDev | Drift |")
            lines.append("| :--- | ---: | ---: | ---: | ---: | ---: |")
            for ch, st in ch_stats.items():
                lines.append(f"| `{ch}` | {st.get('min')} | {st.get('mean')} | {st.get('max')} | {st.get('std')} | {st.get('drift')} |")
            lines.append("")
    else:
        lines.append("_No recorded telemetry run attached._")
        lines.append("")

    # 4. CAN-Bus Health
    can = data.get("can_analysis")
    lines.append("## 4. CAN-Bus Observer Summary")
    if can:
        ba = can.get("bus_analysis") or {}
        lines.append(f"- **Capture #{can.get('capture_id', '—')}** via `{can.get('interface', 'can')}` ({can.get('created_at', '')})")
        lines.append(f"- **Frames:** {can.get('frame_count', 0)} across {ba.get('unique_id_count', len(can.get('unique_ids', [])))} CAN IDs ({ba.get('bus_hz', '—')} fps)")
        dropouts = ba.get("dropout_alerts") or []
        if dropouts:
            lines.append("- **ECU Dropout Alerts:**")
            for da in dropouts:
                lines.append(f"  - `{da.get('arb_id')}`: {da.get('note')}")
        lines.append("")
    else:
        lines.append("_No CAN-bus capture recorded._")
        lines.append("")

    # 5. Service Intervals & Torque Specs
    intervals = data.get("service_intervals") or []
    if intervals:
        lines.append("## 5. Maintenance Schedule")
        lines.append("| Task | Interval | Remaining | Spec | Status |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for iv in intervals:
            rem = f"{iv.get('remaining')} {iv.get('interval_unit', '')}" if iv.get("remaining") is not None else "—"
            spec = f"{iv.get('fluid_or_part', '')} {iv.get('capacity_spec', '')}".strip() or "—"
            lines.append(f"| {iv.get('task_name')} | {iv.get('interval_value')} {iv.get('interval_unit', '')} | {rem} | {spec} | {iv.get('due_state', 'OK')} |")
        lines.append("")

    torques = data.get("torque_specs") or []
    if torques:
        lines.append("## 6. Torque Specifications")
        lines.append("| Component | Spec | Fastener / Sequence |")
        lines.append("| :--- | :--- | :--- |")
        for ts in torques:
            notes = f"{ts.get('fastener_size', '')} {ts.get('sequence_notes', '')}".strip() or "—"
            lines.append(f"| {ts.get('component')} | {ts.get('spec_value')} {ts.get('unit', '')} | {notes} |")
        lines.append("")

    return "\n".join(lines)


def _esc(val: Any) -> str:
    return html.escape(str(val if val is not None else ""), quote=True)


def render_html(data: Dict[str, Any]) -> str:
    a = data.get("asset") or {}
    title = f"{a.get('year', '')} {a.get('make', '')} {a.get('model', '')} — {a.get('name', 'Vehicle / Equipment')}".strip(" —") or "Vehicle Diagnostic Report"

    parts: List[str] = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        f"<title>Shop Bench Report — {_esc(title)}</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0b1120;color:#e2e8f0;margin:0;padding:24px;line-height:1.5;}",
        ".wrap{max-width:960px;margin:0 auto;}",
        "h1{font-size:22px;margin:0 0 6px;color:#38bdf8;}",
        "h2{font-size:15px;margin:22px 0 10px;padding-bottom:4px;border-bottom:1px solid #334155;color:#f8fafc;text-transform:uppercase;letter-spacing:0.06em;}",
        "h3{font-size:14px;margin:12px 0 4px;color:#fde68a;}",
        ".meta{color:#94a3b8;font-size:12px;}",
        ".card{background:#111827;border:1px solid #1f2937;border-radius:8px;padding:12px 14px;margin-bottom:10px;}",
        ".dtc{border-left:4px solid #ef4444;}",
        ".badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700;background:#1e293b;color:#38bdf8;}",
        ".badge.crit{background:#450a0a;color:#fca5a5;}",
        ".badge.warn{background:#451a03;color:#fde68a;}",
        ".badge.ok{background:#064e3b;color:#6ee7b7;}",
        "table{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px;}",
        "th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #1f2937;}",
        "th{color:#94a3b8;text-transform:uppercase;font-size:11px;}",
        "pre{white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12px;background:#020617;padding:10px;border-radius:6px;border:1px solid #1e293b;}",
        "@media print{body{background:#ffffff;color:#111827;padding:10px;}.card,pre{background:#f8fafc;border-color:#cbd5e1;color:#111827;}h1,h2,h3{color:#0f172a;}th,td{border-color:#cbd5e1;}}",
        "</style>",
        "</head>",
        "<body><div class=\"wrap\">",
        f"<h1>Blue Lodge Shop Bench — Diagnostic &amp; Copilot Report</h1>",
        f"<div class=\"meta\"><strong>{_esc(title)}</strong> &middot; VIN/Serial: <code>{_esc(a.get('vin_or_serial') or 'N/A')}</code> &middot; Engine: {_esc(a.get('engine_spec') or 'N/A')} &middot; Meter: {_esc(a.get('meter_value', 0))} {_esc(a.get('meter_unit') or 'mi')} &middot; Generated: {_esc(data.get('generated_at'))}</div>",
    ]
    if a.get("notes"):
        parts.append(f"<div class=\"card\" style=\"margin-top:10px;\"><strong>Vehicle Notes:</strong> {_esc(a.get('notes'))}</div>")

    # 1. DTCs
    parts.append("<h2>1. Diagnostic Trouble Codes (DTCs)</h2>")
    dtcs = data.get("active_dtcs") or []
    if dtcs:
        for d in dtcs:
            sev = str(d.get("severity") or "medium").lower()
            bcls = "crit" if sev in ("critical", "high") else "warn"
            parts.append("<div class=\"card dtc\">")
            parts.append(f"<div><span class=\"badge {bcls}\">{_esc(d.get('code'))}</span> <strong>{_esc(d.get('title'))}</strong> <span class=\"meta\">({_esc(d.get('system', 'ECU'))})</span></div>")
            parts.append(f"<div style=\"margin-top:4px;\"><strong>Driveability:</strong> {_esc(d.get('driveability'))}</div>")
            if d.get("causes"):
                parts.append(f"<div class=\"meta\" style=\"margin-top:4px;\">Common causes: {_esc('; '.join(d['causes']))}</div>")
            parts.append("</div>")
    else:
        parts.append("<div class=\"card\">No active trouble codes recorded.</div>")
    hist = data.get("historical_dtcs") or []
    parts.append(f"<div class=\"meta\">Historical DTCs on file: {_esc(', '.join(hist) if hist else 'None')}</div>")

    # 2. Copilot & Logs
    parts.append("<h2>2. AI Mechanic Copilot Diagnoses &amp; Maintenance Logs</h2>")
    logs = data.get("copilot_and_service_logs") or []
    if logs:
        for lg in logs:
            parts.append("<div class=\"card\">")
            parts.append(f"<h3>[{_esc(str(lg.get('log_type', 'log')).upper())}] {_esc(lg.get('title'))}</h3>")
            parts.append(f"<div class=\"meta\">{_esc(_utc_iso(lg.get('created_at')))}</div>")
            if lg.get("summary"):
                parts.append(f"<pre>{_esc(lg.get('summary'))}</pre>")
            if lg.get("checklist"):
                parts.append("<ul>")
                for item in lg["checklist"]:
                    parts.append(f"<li>{_esc(item)}</li>")
                parts.append("</ul>")
            parts.append("</div>")
    else:
        parts.append("<div class=\"card\">No LLM Mechanic Copilot triage logs saved yet.</div>")

    # 3. Signal Lab
    an = data.get("telemetry_analysis")
    parts.append("<h2>3. Time-Series Telemetry &amp; Signal Correlation Lab</h2>")
    if an and an.get("status") == "ok":
        run = an.get("run") or {}
        vh = an.get("voltage_health") or {}
        o2 = an.get("o2_catalyst_health") or {}
        parts.append("<div class=\"card\">")
        parts.append(f"<div><strong>Run #{_esc(run.get('id'))}</strong>: {_esc(run.get('run_label'))} ({_esc(an.get('sample_count'))} samples / {_esc(an.get('duration_sec'))}s via {_esc(run.get('transport'))})</div>")
        if vh:
            parts.append(f"<div><strong>Voltage &amp; Ripple:</strong> {_esc(vh.get('status'))} — {_esc(vh.get('min_v'))}V to {_esc(vh.get('max_v'))}V (Ripple {_esc(vh.get('ripple_vpp'))} Vpp). {_esc(vh.get('assessment'))}</div>")
        if o2:
            parts.append(f"<div><strong>O2 / Catalyst:</strong> {_esc(o2.get('status'))} — r={_esc(o2.get('o2_correlation_r'))}, Ratio {_esc(o2.get('downstream_to_upstream_ratio'))}. {_esc(o2.get('assessment'))}</div>")
        parts.append("</div>")

        for sg in an.get("signatures") or []:
            bcls = "crit" if sg.get("severity") == "critical" else "warn"
            parts.append("<div class=\"card\">")
            parts.append(f"<span class=\"badge {bcls}\">{_esc(sg.get('code'))}</span> <span class=\"meta\">{_esc(sg.get('confidence'))} CONFIDENCE</span>")
            parts.append(f"<div>{_esc(sg.get('evidence'))}</div>")
            if sg.get("recommended_checks"):
                parts.append(f"<div class=\"meta\">Shop checks: {_esc(' · '.join(sg['recommended_checks']))}</div>")
            parts.append("</div>")

        corrs = an.get("correlations") or []
        if corrs:
            parts.append("<table><thead><tr><th>Signal Pair</th><th>Pearson r</th><th>Interpretation</th></tr></thead><tbody>")
            for c in corrs[:10]:
                parts.append(f"<tr><td>{_esc(c.get('pair'))}</td><td><code>{_esc(c.get('r'))}</code></td><td>{_esc(c.get('interpretation'))}</td></tr>")
            parts.append("</tbody></table>")
    else:
        parts.append("<div class=\"card\">No recorded telemetry run attached.</div>")

    # 4. CAN Bus
    can = data.get("can_analysis")
    parts.append("<h2>4. CAN-Bus Observer Summary</h2>")
    if can:
        ba = can.get("bus_analysis") or {}
        parts.append("<div class=\"card\">")
        parts.append(f"<div>Capture #{_esc(can.get('capture_id'))} ({_esc(can.get('interface'))}) &middot; {_esc(can.get('frame_count'))} frames &middot; {_esc(ba.get('unique_id_count', len(can.get('unique_ids', []))))} CAN IDs &middot; {_esc(ba.get('bus_hz', '—'))} fps</div>")
        for da in ba.get("dropout_alerts") or []:
            parts.append(f"<div style=\"color:#fca5a5;\">&#9888; ECU Dropout on {_esc(da.get('arb_id'))}: {_esc(da.get('note'))}</div>")
        parts.append("</div>")
    else:
        parts.append("<div class=\"card\">No CAN-bus capture recorded.</div>")

    # 5. Intervals & Torque
    intervals = data.get("service_intervals") or []
    if intervals:
        parts.append("<h2>5. Maintenance Schedule</h2><table><thead><tr><th>Task</th><th>Interval</th><th>Remaining</th><th>Spec</th><th>Status</th></tr></thead><tbody>")
        for iv in intervals:
            rem = f"{iv.get('remaining')} {iv.get('interval_unit', '')}" if iv.get("remaining") is not None else "—"
            parts.append(f"<tr><td>{_esc(iv.get('task_name'))}</td><td>{_esc(iv.get('interval_value'))} {_esc(iv.get('interval_unit'))}</td><td>{_esc(rem)}</td><td>{_esc(iv.get('fluid_or_part'))} {_esc(iv.get('capacity_spec'))}</td><td>{_esc(iv.get('due_state', 'OK'))}</td></tr>")
        parts.append("</tbody></table>")

    torques = data.get("torque_specs") or []
    if torques:
        parts.append("<h2>6. Torque Specifications</h2><table><thead><tr><th>Component</th><th>Spec</th><th>Fastener / Sequence</th></tr></thead><tbody>")
        for ts in torques:
            parts.append(f"<tr><td>{_esc(ts.get('component'))}</td><td>{_esc(ts.get('spec_value'))} {_esc(ts.get('unit'))}</td><td>{_esc(ts.get('fastener_size'))} {_esc(ts.get('sequence_notes'))}</td></tr>")
        parts.append("</tbody></table>")

    parts.append("</div></body></html>")
    return "\n".join(parts)


def build_diagnostic_report(
    conn: sqlite3.Connection,
    asset_id: Optional[int] = None,
    run_id: Optional[int] = None,
    fmt: str = "markdown",
    save_file: bool = True,
) -> Dict[str, Any]:
    fmt_clean = str(fmt or "markdown").strip().lower()
    if fmt_clean == "md":
        fmt_clean = "markdown"
    if fmt_clean not in ALLOWED_FORMATS:
        raise ValueError(f"Unsupported report format '{fmt}'. Allowed: {', '.join(ALLOWED_FORMATS)}.")

    data = collect_report_data(conn, asset_id=asset_id, run_id=run_id)
    if fmt_clean == "markdown":
        content = render_markdown(data)
        ext = "md"
        mime = "text/markdown; charset=utf-8"
    elif fmt_clean == "html":
        content = render_html(data)
        ext = "html"
        mime = "text/html; charset=utf-8"
    else:
        content = json.dumps(data, indent=2)
        ext = "json"
        mime = "application/json; charset=utf-8"

    asset_name = str((data.get("asset") or {}).get("name") or "vehicle")
    slug = SLUG_RE.sub("_", asset_name).strip("_").lower()[:40] or "vehicle"
    stamp = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"shop_report_{slug}_{stamp}.{ext}"

    saved_path = ""
    if save_file:
        exp_dir = exports_dir()
        target = (exp_dir / os.path.basename(filename)).resolve()
        if not str(target).startswith(str(exp_dir) + os.sep):
            raise ValueError("Export path escaped exports directory")
        target.write_text(content, encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        saved_path = str(target)

    return {
        "status": "ok",
        "format": fmt_clean,
        "filename": filename,
        "mime_type": mime,
        "file_path": saved_path,
        "size_bytes": len(content.encode("utf-8")),
        "content": content,
    }
