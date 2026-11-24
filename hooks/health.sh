#!/usr/bin/env bash
set -euo pipefail
LODGE_DIR="${LODGE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
MODULE_DIR="${MODULE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODULE_DATA_DIR="${MODULE_DATA_DIR:-$LODGE_DIR/.george/module_data/shop-bench}"
export MODULE_DIR MODULE_DATA_DIR LODGE_DIR
PY="$LODGE_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
"$PY" -c '
import json, sys
sys.path.insert(0, "'"$MODULE_DIR"'/tools")
import shop_db, obd_can
conn = shop_db.get_conn()
assets = shop_db.list_assets(conn)
drv = obd_can._check_optional_drivers()
print(json.dumps({"status": "ok", "module": "shop-bench", "assets": len(assets), "db": shop_db.db_path(), "drivers": drv}))
'
