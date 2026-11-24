#!/usr/bin/env bash
# tools/dispatch.sh — Blue Lodge Native Tool Dispatcher for mod-shop-bench
set -euo pipefail

TOOL_NAME="${1:-}"
ARGS_JSON="${2:-}"
[ -z "$ARGS_JSON" ] && ARGS_JSON="{}"

LODGE_DIR="${LODGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
MODULE_DIR="${MODULE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODULE_DATA_DIR="${MODULE_DATA_DIR:-$LODGE_DIR/.george/module_data/shop-bench}"
export MODULE_DIR MODULE_DATA_DIR LODGE_DIR
mkdir -p "$MODULE_DATA_DIR"

PY="$LODGE_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

case "$TOOL_NAME" in
    shop_ledger_query)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" get_state
        ;;
    shop_asset_save)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" save_asset
        ;;
    shop_spec_save)
        KIND=$(printf '%s' "$ARGS_JSON" | "$PY" -c 'import json,sys; print(json.loads(sys.stdin.read() or "{}").get("kind","interval"))')
        case "$KIND" in
            torque)   printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" save_torque ;;
            part)     printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" save_part ;;
            log)      printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" save_log ;;
            *)        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" save_interval ;;
        esac
        ;;
    shop_obd_discover)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" obd_discover
        ;;
    shop_obd_scan)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" obd_scan
        ;;
    shop_obd_dtc_lookup)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" obd_lookup_dtc
        ;;
    shop_can_decode)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" can_decode
        ;;
    shop_can_observe)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" can_observe
        ;;
    shop_telemetry_analyze)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" telemetry_analyze
        ;;
    shop_telemetry_slice)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" telemetry_slice
        ;;
    shop_vin_decode)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" vin_decode
        ;;
    shop_obd_query)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" obd_query
        ;;
    shop_pid_discover)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" pid_probe_supported
        ;;
    shop_elm_can_sniff)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" elm_can_sniff
        ;;
    shop_freeze_frame_parse)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" obd_parse_freeze_frame
        ;;
    shop_export_report)
        printf '%s' "$ARGS_JSON" | "$PY" "$MODULE_DIR/tools/shop_cli.py" export_report
        ;;
    *)
        printf '{"status":"error","message":"Unknown shop-bench tool: %s"}\n' "$TOOL_NAME"
        exit 1
        ;;
esac
