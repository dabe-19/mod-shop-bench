#!/usr/bin/env bash
# hooks/context.sh — Injects compact Shop Bench asset & due-service summary when workshop keywords match
set -euo pipefail
LODGE_DIR="${LODGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
MODULE_DIR="${MODULE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODULE_DATA_DIR="${MODULE_DATA_DIR:-$LODGE_DIR/.george/module_data/shop-bench}"
export MODULE_DIR MODULE_DATA_DIR LODGE_DIR
PY="$LODGE_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
"$PY" -c '
import sys
sys.path.insert(0, "'"$MODULE_DIR"'/tools")
import shop_db
conn = shop_db.get_conn()
assets = shop_db.list_assets(conn)
runs = shop_db.list_telemetry_runs(conn, limit=1)
lines = []
if assets:
    lines.append("SHOP BENCH ACTIVE ASSETS:")
    for a in assets[:6]:
        b = shop_db.get_asset_bundle(conn, a["id"])
        overdue = [i["task_name"] for i in (b["intervals"] if b else []) if i.get("due_state") == "OVERDUE"]
        dtcs = []
        if b and b["obd_sessions"]:
            dtcs = [d.get("code") for d in (b["obd_sessions"][0].get("dtcs") or []) if d.get("code")]
        extra = []
        if overdue: extra.append(f"OVERDUE: {\", \".join(overdue[:3])}")
        if dtcs: extra.append(f"Last OBD DTCs: {\", \".join(dtcs[:4])}")
        lines.append(f"- [{a[\"id\"]}] {a[\"name\"]} ({a[\"category\"]}, {a[\"year\"]} {a[\"make\"]} {a[\"model\"]}) @ {a[\"meter_value\"]} {a[\"meter_unit\"]}" + (f" | { \" · \".join(extra) }" if extra else ""))
if runs:
    r0 = runs[0]
    dtcs_r = r0.get("active_dtcs") or []
    lines.append(f"SHOP BENCH LIVE TELEMETRY BUFFER: Run #{r0[\"id\"]} ({r0[\"transport\"]}, {r0[\"sample_count\"]}/600 samples buffered, DTCs: {\", \".join(dtcs_r) if dtcs_r else \"none\"}) — call shop_telemetry_analyze(run_id={r0[\"id\"]}) for full multi-channel time-series stats, correlations & fault signatures.")
if not lines:
    print("SHOP BENCH: No assets or telemetry logged yet. Use shop_asset_save or the Shop Bench UI to add vehicles, small engines, HVAC, or tools.")
else:
    print("\n".join(lines))
' 2>/dev/null || true
