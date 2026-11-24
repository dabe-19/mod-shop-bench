#!/usr/bin/env bash
# tools/web_action.sh — Backend JSON action handler for Shop Bench Web UI
set -euo pipefail

ACTION="${1:-get_state}"
BODY="$(cat || true)"
[ -z "$BODY" ] && BODY="{}"

LODGE_DIR="${LODGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
MODULE_DIR="${MODULE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODULE_DATA_DIR="${MODULE_DATA_DIR:-$LODGE_DIR/.george/module_data/shop-bench}"
export MODULE_DIR MODULE_DATA_DIR LODGE_DIR
mkdir -p "$MODULE_DATA_DIR"

PY="$LODGE_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

printf '%s' "$BODY" | "$PY" "$MODULE_DIR/tools/shop_cli.py" "$ACTION"
