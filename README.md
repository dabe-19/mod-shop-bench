# Shop Bench (`mod-shop-bench` v2.6) — Pro Mechanic's Workbench & Fleet Ledger

A zero-API-key, 100% offline-capable Blue Lodge module combining a **local SQLite homestead/fleet maintenance ledger**, a **Torque-style OBD-II / ELM327 / Carly Bluetooth / CAN-bus diagnostic bench**, **web-controlled Windows BT-to-TCP bridging**, **exportable diagnostic & AI Copilot reports**, and multimodal ALP v1 agentic triage loops.

> **Full Setup & Bridge Walkthrough:** See [docs/CARLY_AND_BT_BRIDGE_GUIDE.md](docs/CARLY_AND_BT_BRIDGE_GUIDE.md) for a step-by-step guide to pairing Carly Bluetooth on Windows, how `win_obd_bridge.ps1` works under the hood, running on George (WSL2) vs. Hiram (Rancher Desktop K8s), and exporting PDF/HTML/Markdown diagnostic reports.

## Features

1. **Offline Homestead & Fleet Ledger (SQLite)**:
   - Track **Vehicles, Small Engines, HVAC/Home Systems, Generators, Marine & Shop Tools** with VIN/serial numbers, mileage/hours meters, and uploaded nameplate photos.
   - Manage **Service Intervals** (with due/overdue tracking), **Torque Specs** (`ft-lb`, `Nm`, `in-lb` + sequence notes), **Parts & Consumables Inventory** (OEM vs. aftermarket cross-references & low-stock alerts), and **Maintenance / AI Copilot Logs**.
2. **Pro Code-Reader, Live Gauges & Signal Correlation Lab**:
   - **Zero-dependency core**: Pure Python stdlib POSIX serial (`termios`), TCP ELM327 bridge (`tcp://`), Linux native SocketCAN (`socket.AF_CAN`), 45+ SAE J1979 PID catalog (`0100`–`0160` supported-PID auto-discovery), 110+ SAE J2012 DTC lookup library (`P`/`B`/`C`/`U`) with plain-English driveability safety hints, safe AST **Torque Pro `.csv`**, **OBDb `.json`**, and **`opendbc` `.dbc`** importers.
   - **Time-Series & CAN Signal Lab**: Live SVG radial gauges & multi-channel oscilloscope, Pearson cross-channel correlations, alternator voltage-ripple & brownout detection, upstream/downstream O2 catalyst storage ratios, automated fault signatures, and direct CAN-bus observer (ELM327 `ATH1`/`ATCRA`/`ATMA` & SocketCAN) with ECU dropout & dynamic-byte heatmaps.
   - **Safety First**: Read-only by default. Mode `04` DTC Clear requires explicit operator confirmation (`CLEAR_DTCS`) and snapshots active codes first.
3. **Web-Triggered Windows Bluetooth COM → TCP Bridge**:
   - Built-in **COM Port (`COM5`) + TCP Port (`35000`)** controls right inside **Code Reader & Live Gauges** with **▶ Start Bridge**, **⏹ Stop Bridge**, **Detect COM**, and **Test Link** buttons.
   - On **George (WSL2)**: starts & stops `scripts/win_obd_bridge.ps1` directly via Windows PowerShell interop with clean COM-port release.
   - On **Hiram (Rancher Desktop K8s)**: auto-detects container mode, provides 1-click **Copy Start** & **Copy Stop** PowerShell commands for the Windows host laptop, and connects over `tcp://host.docker.internal:35000`.
4. **Export Diagnostic & LLM-Generated Reports (`.md` / Print `.html` / `.json`)**:
   - Export full vehicle reports bundling DTCs, signal/correlation analytics, CAN-bus health, **AI Mechanic Copilot diagnoses & repair checklists**, service intervals, and torque specs directly from the UI or via the `shop_export_report` agent tool.
5. **ALP v1 Agentic Workflows (200-turn FIFO + ReAct)**:
   - `nameplate_manual_scout`: Parallel vision OCR on uploaded nameplate sticker + local inventory check → ReAct manual/recall/OEM-vs-aftermarket part lookup → saves structured maintenance card.
   - `diagnostic_triage`: Parallel visual inspection (spark plug, belt, PCB) + OBD-II DTC / telemetry / CAN / asset history → ReAct plain-English root-cause diagnosis & step-by-step DIY checklist saved to the vehicle log.

## Quick Start: Carly Bluetooth on George (WSL2) & Hiram (Rancher Desktop)

1. Pair your Carly Classic Bluetooth adapter in **Windows Settings → Bluetooth & devices**, then note its **Outgoing** COM port under **More Bluetooth settings → COM Ports** (e.g. `COM5`).
2. **On George (WSL2):** Open **Shop Bench → Code Reader & Live Gauges**, enter your COM port (`COM5`) & TCP port (`35000`), and click **▶ Start Bridge** → **Test Link**.
3. **On Hiram (Rancher Desktop K8s):** On the Windows laptop host run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\win_obd_bridge.ps1 -ComPort COM5 -TcpPort 35000 -BindAddress 0.0.0.0
   ```
   Then in Shop Bench select **`Windows BT-Bridge / Wi-Fi TCP`**, enter `tcp://host.docker.internal:35000`, and click **Test Link**. Stop anytime with `Ctrl+C` or `.\scripts\win_obd_bridge.ps1 -Stop -TcpPort 35000`.

See [docs/CARLY_AND_BT_BRIDGE_GUIDE.md](docs/CARLY_AND_BT_BRIDGE_GUIDE.md) for complete architecture, script internals, and troubleshooting.
