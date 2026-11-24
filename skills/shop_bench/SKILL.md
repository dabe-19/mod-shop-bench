---
name: shop_bench
description: Homestead & Workshop Telemetry / Mechanic's Workbench (vehicles, small engines, HVAC, tools, OBD-II / Carly BLE / ELM327, and CAN-bus diagnostics).
---

# Shop Bench Skill

Use the `+shop` tools to manage offline maintenance records and diagnose mechanical or electrical faults:
- `shop_ledger_query`: Inspect vehicles, small engines, HVAC units, due/overdue service intervals, torque specs, parts stock, and saved OBD/CAN sessions.
- `shop_asset_save` / `shop_spec_save`: Persist structured equipment cards, service intervals, torque specs, OEM-vs-aftermarket part cross-references, and diagnostic logs.
- `shop_vin_decode`: Decode any 17-char VIN via NHTSA vPIC (free, zero keys, offline WMI fallback) into exact Year/Make/Model/Engine/Turbo/GDI architecture notes before diagnosing.
- `shop_telemetry_analyze` / `shop_telemetry_slice`: Analyze the rolling 600-sample SQLite time-series buffer across ALL captured core + rotating rich ECU PIDs (including operating-regime snapshots: idle vs peak RPM vs peak load vs peak trim vs min fuel pressure) and slice/filter raw multi-channel rows to falsify hypotheses.
- `shop_obd_discover` / `shop_pid_discover` / `shop_obd_scan` / `shop_obd_query` / `shop_obd_dtc_lookup` / `shop_freeze_frame_parse` / `shop_can_observe` / `shop_elm_can_sniff` / `shop_can_decode`: Discover USB/BT/Carly-BLE/SocketCAN adapters, query supported ECU PID bitmasks (`0100`–`0180`), inspect multiple CAN ECUs (`7E8` ECM, `7EA` TCM, readiness monitors, calibration IDs) in strictly read-only mode, scan Mode 01/03/07/09 telemetry, decode SAE J2012 DTCs, parse freeze-frame CSVs, sniff live ELM327 CAN frames, and decode `candump` + DBC logs.
- Always pair photos of nameplates, spark plugs, belts, or circuit boards with `vision_analyze` before concluding root cause.
