#!/usr/bin/env bash
# commands/shop.sh — /shop slash command for Shop Bench
set -euo pipefail
LODGE_DIR="${LODGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
MODULE_DIR="${MODULE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="$LODGE_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

cmd_shop() {
    local sub="${1:-status}"
    shift || true
    case "$sub" in
        status|list)
            "$PY" "$MODULE_DIR/tools/shop_cli.py" get_state <<<'{}'
            ;;
        discover|adapters)
            "$PY" "$MODULE_DIR/tools/shop_cli.py" obd_discover <<<'{}'
            ;;
        scan)
            local profile="${1:-lean_misfire}"
            "$PY" "$MODULE_DIR/tools/shop_cli.py" obd_scan <<<"{\"transport\":\"sim\",\"sim_profile\":\"$profile\"}"
            ;;
        dtc)
            local code="${1:-P0171}"
            "$PY" "$MODULE_DIR/tools/shop_cli.py" obd_lookup_dtc <<<"{\"codes\":[\"$code\"]}"
            ;;
        *)
            echo "Usage: /shop [status | discover | scan [healthy|lean_misfire|can_bus_fault] | dtc <CODE>]"
            ;;
    esac
}
