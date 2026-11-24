# Carly Bluetooth OBD-II & Windows COM → TCP Bridge Guide (`mod-shop-bench v2.6`)

This guide explains how to connect a **Carly Classic Bluetooth SPP** (or any ELM327/STN Bluetooth) OBD-II adapter from a Windows laptop into **Blue Lodge Shop Bench** running on:
- **George** (WSL2 on Windows — supports 1-click Start & Stop right from the Shop Bench webpage), or
- **Hiram** (Rancher Desktop Kubernetes inside WSL2 on a laptop — connects via `tcp://host.docker.internal:35000`).

---

## 1. Why the Bridge Exists (Architecture)

WSL2 Linux distributions and Rancher Desktop Kubernetes pods do not have direct access to your laptop's internal Bluetooth radio without low-level USB controller passthrough (`usbipd-win`, which breaks Windows Bluetooth for the rest of the laptop).

Because your Carly adapter pairs cleanly with **Torque Pro**, it uses **Classic Bluetooth Serial Port Profile (SPP)** rather than locked proprietary BLE. Windows automatically binds Classic Bluetooth SPP devices to a standard virtual serial port (e.g., `COM5`).

Our zero-dependency bridge script (`scripts/win_obd_bridge.ps1`) runs natively in Windows PowerShell using built-in .NET classes (`System.IO.Ports.SerialPort` + `System.Net.Sockets.TcpListener`) — **no Python installation or admin driver hacks required on Windows**:

```text
┌────────────────────┐   Classic BT SPP   ┌───────────────────────────────┐
│ Vehicle OBD-II Port│ ◄────────────────► │ Windows Bluetooth Stack       │
│ + Carly Adapter    │   38400 8N1 ELM327 │ Outgoing Virtual Port (COM5)  │
└────────────────────┘                    └──────────────┬────────────────┘
                                                         │ SerialPort.Open()
                                          ┌──────────────▼────────────────┐
                                          │ scripts/win_obd_bridge.ps1    │
                                          │ Bidirectional Byte Pump (15ms)│
                                          │ PID + StopFlag Clean Shutdown │
                                          └──────────────┬────────────────┘
                                                         │ TCP :35000
                                          ┌──────────────▼────────────────┐
                                          │ Blue Lodge Shop Bench         │
                                          │ • George (WSL2): 127.0.0.1    │
                                          │ • Hiram (K8s):                │
                                          │   host.docker.internal:35000  │
                                          │ TcpSocketELM327 → Gauges / AI │
                                          └───────────────────────────────┘
```

---

## 2. How `scripts/win_obd_bridge.ps1` Works Under the Hood

Understanding each stage of the script makes troubleshooting effortless in the garage:

1. **Strict Parameter Validation (Fail-Closed)**:
   - `-ComPort` (default `COM5`) must match `^COM[1-9][0-9]{0,2}$`.
   - `-TcpPort` (default `35000`) must be in `1024..65535`.
   - `-BindAddress` (default `127.0.0.1`) is restricted to `127.0.0.1` (WSL2 mirrored loopback) or `0.0.0.0` (required for Rancher Desktop `host.docker.internal` or WSL2 NAT).
   - `-BaudRate` (default `38400`) is restricted to standard ELM327 rates (`9600`, `19200`, `38400`, `57600`, `115200`).
2. **PID Tracking & Stop-Flag Lifecycle**:
   - On startup, if `-PidFile` is passed (automatically provided when launched from the Shop Bench webpage on WSL2), the script writes its exact Windows `$PID` to `.george/module_data/shop-bench/bt_bridge.pid` and clears any stale `-StopFlagFile`.
   - The accept loop uses non-blocking `$listener.Pending()` with a `150ms` sleep rather than blocking forever inside `AcceptTcpClient()`. That allows it to check `Test-StopRequested` (existence of `-StopFlagFile`) every `150ms`.
3. **Lazy COM-Port Opening (No Bluetooth Lock-Out)**:
   - The script starts listening on TCP immediately, **without holding `COM5` open while idle**.
   - Only when Shop Bench opens a TCP connection (for a health scan, live gauge stream, or PID probe) does the script call `$sp.Open()`.
4. **Bidirectional Raw Byte Pump (`15ms` Loop)**:
   - While `$client.Connected` and no stop flag is present:
     - Bytes arriving from Shop Bench over TCP (`$stream.DataAvailable`) are written straight to `$sp.Write(...)` (e.g. `ATZ`, `010C`, `03`, `ATMA`).
     - Bytes arriving from the Carly adapter over Bluetooth (`$sp.BytesToRead > 0`) are written straight back to `$stream.Write(...)`.
   - Half-close detection (`$client.Client.Poll(0, SelectRead)` with `Available -eq 0`) notices immediately when Shop Bench finishes a scan and closes the socket.
5. **Guaranteed `finally` Cleanup & Stopping the Bridge**:
   - Whenever a scan disconnects, **⏹ Stop Bridge** is clicked, `Ctrl+C` is pressed, or `-Stop` is run:
     - `$sp.Close()` + `$sp.Dispose()` runs unconditionally inside `finally`, immediately releasing Windows `COM5` so Bluetooth doesn't stay wedged.
     - `$listener.Stop()` frees TCP port `35000`, and PID / stop-flag files are cleaned up.
   - Running `.\win_obd_bridge.ps1 -Stop -TcpPort 35000` signals the stop flag (if present) and stops any bridge process listening on that TCP port.

> **Python Alternative:** `scripts/win_obd_bridge.py` implements the exact same bridge & `--pid-file` / `--stop-flag` lifecycle if you prefer running under Windows Python (`python scripts\win_obd_bridge.py --com COM5 --port 35000`).

---

## 3. Step-by-Step Pairing & Finding Your Outgoing COM Port (Windows)

1. Plug your **Carly Bluetooth adapter** into the vehicle's 16-pin OBD-II port (under the steering column) and turn the ignition to **Key-On Engine-Off (KOEO)** or start the engine.
2. On Windows, open **Settings → Bluetooth & devices → Add device → Bluetooth** and pair the Carly adapter (typical PIN if prompted: `1234` or `0000`).
3. Open **Settings → Bluetooth & devices → Devices → More Bluetooth settings** (or press `Win+R` and run `bthprops.cpl`, then click **More Bluetooth options**).
4. Click the **COM Ports** tab.
5. Note the **`Outgoing`** COM port assigned to your Carly adapter (for example, `COM5`).
   - **Important:** Windows often creates *two* entries (an `Incoming` port and an `Outgoing` port). Always choose the **`Outgoing`** port (usually labeled with `Dev B` / `SPP`).

---

## 4. Starting & Stopping the Bridge from the Shop Bench Webpage

Open **Shop Bench → Code Reader & Live Gauges**. Inside **Adapter Connection & Live Recording**, you will see the **Windows Bluetooth → TCP Bridge** strip pre-filled with defaults (`COM5` and TCP `35000`):

### Option A: Running on George (WSL2 on Windows) — 1-Click Web Control
1. Click **Detect COM** to list active Windows COM ports directly from WSL2, or type your Outgoing COM port (default `COM5`) and TCP port (default `35000`).
2. Leave Bind at `127.0.0.1 (WSL2 Local)` (or switch to `0.0.0.0` if using WSL2 NAT).
3. Click **▶ Start Bridge**.
   - Shop Bench invokes `powershell.exe` safely (strict `^COM[1-9][0-9]{0,2}$` allowlist, zero shell interpolation), spawns `win_obd_bridge.ps1` detached, tracks its Windows PID, flips the status badge to **`RUNNING (PID ...)`**, and automatically switches your Transport to **`Windows BT-Bridge / Wi-Fi TCP`** (`tcp://127.0.0.1:35000`).
4. Click **Test Link** to run an `ATI` + `ATRV` ELM327 handshake and confirm adapter version & live battery voltage.
5. Click **1. Full Health & Code Scan** or **▶ Start Live Stream & Log** to monitor gauges and record telemetry!
6. When done in the garage, click **⏹ Stop Bridge** — this drops the stop-flag, waits for clean COM-port release, and frees TCP `35000`.

### Option B: Running on Hiram (Rancher Desktop K8s inside WSL2 on Laptop)
Because Hiram runs inside a Linux Kubernetes pod inside Rancher Desktop, the container cannot directly spawn Windows `powershell.exe` on the host. Shop Bench detects container mode automatically (`HOST BRIDGE MODE`) and shows 1-click **Copy Start** and **Copy Stop** buttons pre-filled with your COM & TCP port:
1. On your Windows laptop, open **PowerShell** in the `mod-shop-bench\scripts` directory (or copy `scripts\win_obd_bridge.ps1` to your Desktop).
2. Paste & run the Start command (note `-BindAddress 0.0.0.0` so Rancher's VM can reach Windows via `host.docker.internal`):
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\win_obd_bridge.ps1 -ComPort COM5 -TcpPort 35000 -BindAddress 0.0.0.0
   ```
   *(If Windows Defender Firewall prompts on first run, allow access on **Private** networks so the Rancher VM can connect.)*
3. In Shop Bench on Hiram:
   - Transport: **`Windows BT-Bridge / Wi-Fi TCP (Carly & WSL2/Rancher)`**
   - Address: **`tcp://host.docker.internal:35000`**
   - Click **Test Link** to verify the live ELM327 handshake!
4. To stop the bridge on Windows, press **`Ctrl+C`** in the PowerShell window or run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\win_obd_bridge.ps1 -Stop -TcpPort 35000
   ```

---

## 5. Exporting Diagnostic & LLM-Generated Reports

You can export a full vehicle diagnostic dossier at any time from either:
- **Code Reader & Live Gauges → Signal & Correlation Diagnostics**, or
- **Mechanic Copilot → Saved Diagnostic & AI Copilot Reports**, or
- Asking the agent directly (`shop_export_report` tool in the `+shop` bundle).

Available formats:
| Button / Format | File Type | Best For |
| :--- | :--- | :--- |
| **⬇ Export Report (.md)** | Markdown (`.md`) | Archiving in Git, sharing on Discord/GitHub, feeding to another LLM |
| **⬇ Print HTML (.html)** | Self-contained Print-Ready HTML (`.html`) | Opening in browser → `Ctrl+P` → **Save as PDF** to hand to a shop or keep in your glovebox folder |
| **⬇ JSON (.json)** | Structured JSON (`.json`) | Raw archival / downstream scripting |

Every exported report bundles:
1. Vehicle Year / Make / Model / VIN / Engine Spec / Odometer
2. Active & Historical DTCs with plain-English meanings, driveability safety hints & common causes
3. All **AI Mechanic Copilot** diagnoses, plain-English explanations & step-by-step repair checklists
4. Recorded Time-Series Telemetry stats, Voltage & Alternator Ripple Health, O2 Catalyst Health, Pearson cross-channel correlations & automated fault signatures
5. CAN-Bus Observer health (frame rate, ECU dropout alerts, dynamic byte heatmap)
6. Maintenance Intervals & Torque Specs

A backup copy is also persisted locally under `.george/module_data/shop-bench/exports/` (`0600` permissions).

---

## 6. Security Boundaries

- **SSRF Guard (`tools/obd_can.py`)**: Shop Bench's `tcp://` ELM327 transport strictly permits only `localhost`, `127.0.0.1`, `host.docker.internal`, and RFC1918 private LAN IPs (`10.x`, `172.16-31.x`, `192.168.x`), explicitly blocking link-local metadata (`169.254.169.254`) and public internet IPs.
- **Command Safety (`tools/bt_bridge.py`)**: All web inputs are validated against strict allowlists (`^COM[1-9][0-9]{0,2}$`, `1024..65535`, `127.0.0.1|0.0.0.0`), executed with fixed binary paths and `shell=False`.
- **Read-Only by Default**: OBD-II scans and CAN sniffs are read-only; Mode `04` DTC Clear requires typing `CLEAR_DTCS` and snapshots codes first.

---

## 7. Quick Troubleshooting

| Symptom | Likely Cause | Fix |
| :--- | :--- | :--- |
| `Test Link` says **Could not reach tcp://host.docker.internal:35000** | Bridge bound to `127.0.0.1` instead of `0.0.0.0` (Hiram/Rancher) or Windows Firewall blocked TCP 35000 | Restart with `-BindAddress 0.0.0.0` and allow Private network in Windows Firewall |
| TCP connects, but ELM327 handshake fails / times out | Picked the **Incoming** Bluetooth COM port instead of **Outgoing**, or car ignition is OFF | Check `More Bluetooth settings → COM Ports` for the **Outgoing** port; turn key to **KOEO** |
| `Access to the port 'COM5' is denied` | Another app (Torque, previous PowerShell window) still holds `COM5` | Click **⏹ Stop Bridge** or run `.\win_obd_bridge.ps1 -Stop -TcpPort 35000` |
| Garbled bytes from ELM327 | Adapter uses `115200` baud instead of `38400` | Restart bridge with `-BaudRate 115200` |
| `BUFFER FULL` during ELM327 CAN Sniff | Raw 500 kbps bus exceeds Bluetooth SPP bandwidth | Enter a 3-char hex CAN ID filter (e.g. `201` or `7E8`) in **Optional ATCRA Filter** |
