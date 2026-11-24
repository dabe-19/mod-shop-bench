#!/usr/bin/env python3
"""
tools/bt_bridge.py — Safe Windows Bluetooth COM -> TCP Bridge controller for mod-shop-bench.

Allows starting, stopping, and inspecting scripts/win_obd_bridge.ps1 directly from the Shop Bench
web UI on WSL2 (George) while gracefully providing live TCP link testing and 1-click copyable
PowerShell Start/Stop commands inside Rancher Desktop / Kubernetes containers (Hiram).

Security guarantees:
- Strict allow-list validation on COM port (^COM[1-9][0-9]{0,2}$), TCP port (1024..65535),
  bind address (127.0.0.1 | 0.0.0.0), and baud rate (9600..115200).
- Zero shell=True invocations; fixed binary paths (/mnt/c/WINDOWS/.../powershell.exe, /usr/bin/wslpath).
- Strict canonical path containment for PID, stop-flag, and state files inside MODULE_DATA_DIR.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import obd_can
import shop_db

COM_PORT_RE = re.compile(r"^COM([1-9][0-9]{0,2})$", re.IGNORECASE)
ALLOWED_BINDS = ("127.0.0.1", "0.0.0.0")
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "host.docker.internal", "host.rancher-desktop.internal", "host.lima.internal")
ALLOWED_BAUDS = (9600, 19200, 38400, 57600, 115200)

MODULE_DIR = Path(__file__).resolve().parents[1]
PS_SCRIPT_PATH = (MODULE_DIR / "scripts" / "win_obd_bridge.ps1").resolve()

POWERSHELL_CANDIDATES = (
    Path("/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe"),
    Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
)
WSLPATH_BIN = Path("/usr/bin/wslpath")


def validate_com_port(com_port: Any) -> str:
    raw = str(com_port or "COM5").strip().upper()
    if not COM_PORT_RE.match(raw):
        raise ValueError(f"Invalid COM port '{raw}'. Expected format COM1..COM999 (e.g. COM5).")
    return raw


def validate_tcp_port(tcp_port: Any) -> int:
    try:
        p = int(tcp_port if tcp_port not in (None, "") else 35000)
    except (TypeError, ValueError) as exc:
        raise ValueError("TCP port must be an integer between 1024 and 65535.") from exc
    if not (1024 <= p <= 65535):
        raise ValueError(f"TCP port must be between 1024 and 65535 (got {p}).")
    return p


def _is_safe_local_ipv4(raw: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(raw)
        if ip.is_link_local or ip.is_multicast:
            return False
        # Allow loopback, unspecified (0.0.0.0), RFC1918 private LAN, and Tailscale CGNAT (100.64.0.0/10)
        return ip.is_loopback or ip.is_unspecified or ip.is_private or (ip in ipaddress.IPv4Network("100.64.0.0/10"))
    except ValueError:
        return False


def validate_bind_address(bind_address: Any) -> str:
    raw = str(bind_address or "127.0.0.1").strip()
    if raw in ALLOWED_BINDS or _is_safe_local_ipv4(raw):
        return raw
    raise ValueError(
        f"Bind address must be 127.0.0.1, 0.0.0.0, or a private LAN/Tailscale IPv4 address (got '{raw}')."
    )


def validate_target_host(target_host: Any, fallback: str = "127.0.0.1") -> str:
    raw = str(target_host or "").strip().lower()
    if not raw:
        return fallback
    if raw.startswith("tcp://"):
        raw = raw[6:].split(":", 1)[0].strip()
    if raw in ALLOWED_HOSTS:
        return raw
    try:
        ip = ipaddress.IPv4Address(raw)
        if not ip.is_unspecified and _is_safe_local_ipv4(raw):
            return raw
    except ValueError:
        pass
    raise ValueError(
        f"Target host must be localhost, host.docker.internal, or a private LAN/Tailscale IPv4 address (got '{raw}')."
    )


def validate_baud_rate(baud_rate: Any) -> int:
    try:
        b = int(baud_rate if baud_rate not in (None, "") else 38400)
    except (TypeError, ValueError) as exc:
        raise ValueError("Baud rate must be an integer.") from exc
    if b not in ALLOWED_BAUDS:
        raise ValueError(f"Unsupported baud rate {b}. Allowed: {', '.join(str(x) for x in ALLOWED_BAUDS)}.")
    return b


def _safe_data_file(name: str) -> Path:
    base = shop_db.data_dir().resolve()
    target = (base / os.path.basename(name)).resolve()
    if not str(target).startswith(str(base) + os.sep):
        raise ValueError("Bridge state file escaped module data directory")
    return target


def _state_file() -> Path:
    return _safe_data_file("bt_bridge.json")


def _pid_file() -> Path:
    return _safe_data_file("bt_bridge.pid")


def _stop_flag_file() -> Path:
    return _safe_data_file("bt_bridge.stop")


def _find_powershell() -> Optional[Path]:
    for cand in POWERSHELL_CANDIDATES:
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    return None


def _is_container() -> bool:
    if Path("/.dockerenv").exists() or os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    try:
        cg = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
        if any(tok in cg for tok in ("docker", "kubepods", "containerd", "rancher")):
            return True
    except OSError:
        pass
    return False


def _wslpath_win(path: Path) -> str:
    if not WSLPATH_BIN.is_file():
        return str(path)
    out = subprocess.run(
        [str(WSLPATH_BIN), "-w", str(path)],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return out.stdout.strip()


def _load_saved_state() -> Dict[str, Any]:
    sf = _state_file()
    if not sf.is_file():
        return {}
    try:
        data = json.loads(sf.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(data: Dict[str, Any]) -> None:
    sf = _state_file()
    sf.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(sf, 0o600)
    except OSError:
        pass


def _is_windows_pid_alive(ps_bin: Path, pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        res = subprocess.run(
            [
                str(ps_bin),
                "-NoProfile",
                "-Command",
                f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{ 'ALIVE' }} else {{ 'DEAD' }}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "ALIVE" in (res.stdout or "")
    except Exception:
        return False


def _probe_tcp_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def build_windows_commands(
    com_port: str = "COM5",
    tcp_port: int = 35000,
    bind_address: str = "127.0.0.1",
    baud_rate: int = 38400,
) -> Dict[str, str]:
    com = validate_com_port(com_port)
    port = validate_tcp_port(tcp_port)
    bind = validate_bind_address(bind_address)
    baud = validate_baud_rate(baud_rate)
    rel_script = ".george/modules/shop-bench/scripts/win_obd_bridge.ps1"
    start_wsl = (
        f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w {rel_script})" '
        f'-ComPort {com} -TcpPort {port} -BindAddress {bind} -BaudRate {baud}'
    )
    start_win = (
        f"$s=(wsl.exe bash -lc 'wslpath -w ~/projects/blue-lodge/{rel_script}' 2>$null).Trim(); "
        f"if(-not $s){{$s='.\\win_obd_bridge.ps1'}}; "
        f"powershell -NoProfile -ExecutionPolicy Bypass -File $s -ComPort {com} -TcpPort {port} -BindAddress {bind} -BaudRate {baud}"
    )
    stop_wsl = (
        f'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(wslpath -w {rel_script})" '
        f'-Stop -TcpPort {port}'
    )
    stop_win = (
        f"$s=(wsl.exe bash -lc 'wslpath -w ~/projects/blue-lodge/{rel_script}' 2>$null).Trim(); "
        f"if(-not $s){{$s='.\\win_obd_bridge.ps1'}}; "
        f"powershell -NoProfile -ExecutionPolicy Bypass -File $s -Stop -TcpPort {port}"
    )
    return {
        "start_ps1": start_win,
        "start_ps1_rancher": start_win,
        "start_wsl": start_wsl,
        "stop_ps1": stop_win,
        "stop_wsl": stop_wsl,
        "start_py": f"python win_obd_bridge.py --com {com} --port {port} --bind {bind} --baud {baud}",
    }


def detect_environment(tcp_port: int = 35000) -> Dict[str, Any]:
    port = validate_tcp_port(tcp_port)
    ps_bin = _find_powershell()
    in_container = _is_container()
    can_spawn = bool(ps_bin and WSLPATH_BIN.is_file() and PS_SCRIPT_PATH.is_file() and not in_container)
    if can_spawn:
        mode = "wsl2_interop"
        rec_host = "127.0.0.1"
        rec_bind = "127.0.0.1"
    elif in_container:
        mode = "container_host_bridge"
        rec_host = "host.docker.internal"
        rec_bind = "0.0.0.0"
    else:
        mode = "native_linux"
        rec_host = "127.0.0.1"
        rec_bind = "127.0.0.1"
    return {
        "mode": mode,
        "can_spawn_windows": can_spawn,
        "in_container": in_container,
        "powershell_available": bool(ps_bin),
        "recommended_host": rec_host,
        "recommended_bind": rec_bind,
        "recommended_tcp_url": f"tcp://{rec_host}:{port}",
        "script_path": str(PS_SCRIPT_PATH),
    }


def list_windows_com_ports() -> Dict[str, Any]:
    ps_bin = _find_powershell()
    env = detect_environment()
    if not ps_bin or not env["can_spawn_windows"]:
        return {
            "status": "ok",
            "ports": [],
            "environment": env,
            "message": "Windows PowerShell interop is not available inside this container/OS. On your Windows laptop, open Settings -> Bluetooth -> More Bluetooth settings -> COM Ports to see your Carly Outgoing COM port (e.g. COM5).",
        }
    try:
        res = subprocess.run(
            [
                str(ps_bin),
                "-NoProfile",
                "-Command",
                "[System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object -Unique",
            ],
            capture_output=True,
            text=True,
            timeout=6,
        )
        ports: List[str] = []
        for line in (res.stdout or "").splitlines():
            cand = line.strip().upper()
            if COM_PORT_RE.match(cand) and cand not in ports:
                ports.append(cand)
        return {
            "status": "ok",
            "ports": ports,
            "environment": env,
            "message": f"Detected {len(ports)} Windows COM port(s): {', '.join(ports)}" if ports else "No active Windows COM ports reported. Pair your Carly adapter in Windows Bluetooth Settings first.",
        }
    except Exception as exc:
        return {
            "status": "error",
            "ports": [],
            "environment": env,
            "message": f"Failed to query Windows COM ports: {exc}",
        }


def bridge_status(
    com_port: Optional[str] = None,
    tcp_port: Optional[int] = None,
    bind_address: Optional[str] = None,
    probe_tcp: bool = False,
    target_host: Optional[str] = None,
) -> Dict[str, Any]:
    saved = _load_saved_state()
    com = validate_com_port(com_port or saved.get("com_port") or "COM5")
    port = validate_tcp_port(tcp_port if tcp_port not in (None, "") else saved.get("tcp_port", 35000))
    env = detect_environment(port)
    bind = validate_bind_address(bind_address or saved.get("bind_address") or env["recommended_bind"])
    baud = validate_baud_rate(saved.get("baud_rate") or 38400)
    default_host = bind if (bind not in ("0.0.0.0", "127.0.0.1")) else (saved.get("target_host") or env["recommended_host"])
    eff_host = validate_target_host(target_host or default_host, env["recommended_host"])

    pid = int(saved.get("pid") or 0)
    pf = _pid_file()
    if pf.is_file():
        try:
            pid_txt = pf.read_text(encoding="ascii", errors="ignore").strip()
            if pid_txt.isdigit():
                pid = int(pid_txt)
        except OSError:
            pass

    running = False
    ps_bin = _find_powershell()
    if pid > 0 and ps_bin and env["can_spawn_windows"]:
        running = _is_windows_pid_alive(ps_bin, pid)
        if not running:
            # Stale PID file cleanup
            pid = 0
            if pf.is_file():
                try:
                    pf.unlink()
                except OSError:
                    pass

    tcp_reachable: Optional[bool] = None
    if probe_tcp:
        tcp_reachable = _probe_tcp_open(eff_host, port, timeout=0.8)
        if tcp_reachable and not running:
            running = True

    state_label = "RUNNING" if running else ("HOST_MODE" if env["in_container"] else "STOPPED")
    return {
        "status": "ok",
        "running": running,
        "state": state_label,
        "pid": pid if running else None,
        "com_port": com,
        "tcp_port": port,
        "bind_address": bind,
        "target_host": eff_host,
        "baud_rate": baud,
        "tcp_url": f"tcp://{eff_host}:{port}",
        "tcp_reachable": tcp_reachable,
        "environment": env,
        "commands": build_windows_commands(com, port, bind, baud),
    }


def start_bridge(
    com_port: Any = "COM5",
    tcp_port: Any = 35000,
    bind_address: Any = None,
    baud_rate: Any = 38400,
    target_host: Optional[str] = None,
) -> Dict[str, Any]:
    com = validate_com_port(com_port)
    port = validate_tcp_port(tcp_port)
    env = detect_environment(port)
    bind = validate_bind_address(bind_address or env["recommended_bind"])
    baud = validate_baud_rate(baud_rate)
    saved = _load_saved_state()
    default_host = bind if (bind not in ("0.0.0.0", "127.0.0.1")) else (saved.get("target_host") or env["recommended_host"])
    eff_host = validate_target_host(target_host or default_host, env["recommended_host"])
    cmds = build_windows_commands(com, port, bind, baud)

    ps_bin = _find_powershell()
    if not ps_bin or not env["can_spawn_windows"]:
        reachable = _probe_tcp_open(eff_host, port, timeout=0.8)
        _save_state({
            "com_port": com,
            "tcp_port": port,
            "bind_address": bind,
            "target_host": eff_host,
            "baud_rate": baud,
            "pid": 0,
            "updated_at": shop_db.now_ts(),
        })
        if reachable:
            return {
                "status": "ok",
                "running": True,
                "state": "RUNNING",
                "com_port": com,
                "tcp_port": port,
                "bind_address": bind,
                "target_host": eff_host,
                "baud_rate": baud,
                "tcp_url": f"tcp://{eff_host}:{port}",
                "tcp_reachable": True,
                "environment": env,
                "commands": cmds,
                "message": f"Windows host bridge is already reachable at tcp://{eff_host}:{port}!",
            }
        return {
            "status": "host_action_required",
            "running": False,
            "state": "HOST_MODE",
            "com_port": com,
            "tcp_port": port,
            "bind_address": bind,
            "target_host": eff_host,
            "baud_rate": baud,
            "tcp_url": f"tcp://{eff_host}:{port}",
            "tcp_reachable": False,
            "environment": env,
            "commands": cmds,
            "message": f"Container mode ({env['mode']}): Run the copied PowerShell Start command on your Windows host laptop to bridge {com} -> {bind}:{port}, then click Test Link (tcp://{eff_host}:{port}).",
        }

    # Stop any previously tracked bridge cleanly first
    stop_bridge()

    pf = _pid_file()
    sf = _stop_flag_file()
    for p in (pf, sf):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    win_script = _wslpath_win(PS_SCRIPT_PATH)
    win_pid = _wslpath_win(pf)
    win_stop = _wslpath_win(sf)

    # Spawn detached PowerShell bridge with zero shell=True
    proc = subprocess.Popen(
        [
            str(ps_bin),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            win_script,
            "-ComPort",
            com,
            "-TcpPort",
            str(port),
            "-BindAddress",
            bind,
            "-BaudRate",
            str(baud),
            "-PidFile",
            win_pid,
            "-StopFlagFile",
            win_stop,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait briefly for PowerShell to write its exact Windows PID file
    win_pid_val = 0
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if pf.is_file():
            txt = pf.read_text(encoding="ascii", errors="ignore").strip()
            if txt.isdigit():
                win_pid_val = int(txt)
                break
        if proc.poll() is not None:
            break
        time.sleep(0.15)

    alive = bool(win_pid_val and _is_windows_pid_alive(ps_bin, win_pid_val))
    _save_state({
        "com_port": com,
        "tcp_port": port,
        "bind_address": bind,
        "baud_rate": baud,
        "pid": win_pid_val if alive else 0,
        "started_at": shop_db.now_ts(),
    })

    if not alive:
        return {
            "status": "error",
            "running": False,
            "state": "STOPPED",
            "com_port": com,
            "tcp_port": port,
            "bind_address": bind,
            "baud_rate": baud,
            "tcp_url": f"tcp://{env['recommended_host']}:{port}",
            "environment": env,
            "commands": cmds,
            "message": f"Bridge exited before binding TCP {port}. Ensure TCP {port} is free and run '{cmds['start_ps1']}' in PowerShell to inspect any Windows firewall prompt.",
        }

    return {
        "status": "ok",
        "running": True,
        "state": "RUNNING",
        "pid": win_pid_val,
        "com_port": com,
        "tcp_port": port,
        "bind_address": bind,
        "baud_rate": baud,
        "tcp_url": f"tcp://{env['recommended_host']}:{port}",
        "environment": env,
        "commands": cmds,
        "message": f"Windows BT-OBD Bridge started (Windows PID {win_pid_val}): {com} -> tcp://{bind}:{port}. Ready for Shop Bench!",
    }


def stop_bridge(tcp_port: Optional[int] = None) -> Dict[str, Any]:
    saved = _load_saved_state()
    com = validate_com_port(saved.get("com_port") or "COM5")
    port = validate_tcp_port(tcp_port if tcp_port not in (None, "") else saved.get("tcp_port", 35000))
    env = detect_environment(port)
    bind = validate_bind_address(saved.get("bind_address") or env["recommended_bind"])
    baud = validate_baud_rate(saved.get("baud_rate") or 38400)
    cmds = build_windows_commands(com, port, bind, baud)

    pf = _pid_file()
    sf = _stop_flag_file()

    # Always write stop flag first so a running bridge closes SerialPort + TcpListener gracefully
    sf.write_text("stop", encoding="ascii")

    pid = int(saved.get("pid") or 0)
    if pf.is_file():
        txt = pf.read_text(encoding="ascii", errors="ignore").strip()
        if txt.isdigit():
            pid = int(txt)

    ps_bin = _find_powershell()
    stopped_pid: Optional[int] = None
    if ps_bin and env["can_spawn_windows"]:
        # Give graceful stop loop ~500ms to close COM port cleanly
        time.sleep(0.5)
        if pid > 0 and _is_windows_pid_alive(ps_bin, pid):
            subprocess.run(
                [
                    str(ps_bin),
                    "-NoProfile",
                    "-Command",
                    f"Stop-Process -Id {int(pid)} -Force -ErrorAction SilentlyContinue",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            stopped_pid = pid
        elif pid > 0:
            stopped_pid = pid
    else:
        return {
            "status": "host_action_required",
            "running": False,
            "state": "HOST_MODE",
            "com_port": com,
            "tcp_port": port,
            "bind_address": bind,
            "baud_rate": baud,
            "tcp_url": f"tcp://{env['recommended_host']}:{port}",
            "environment": env,
            "commands": cmds,
            "message": f"Container mode: press Ctrl+C in your Windows PowerShell bridge window or run '{cmds['stop_ps1']}' to stop and release {com}.",
        }

    for p in (pf, sf):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    _save_state({
        "com_port": com,
        "tcp_port": port,
        "bind_address": bind,
        "baud_rate": baud,
        "pid": 0,
        "stopped_at": shop_db.now_ts(),
    })

    return {
        "status": "ok",
        "running": False,
        "state": "STOPPED",
        "stopped_pid": stopped_pid,
        "com_port": com,
        "tcp_port": port,
        "bind_address": bind,
        "baud_rate": baud,
        "tcp_url": f"tcp://{env['recommended_host']}:{port}",
        "environment": env,
        "commands": cmds,
        "message": f"Bridge stopped cleanly (PID {stopped_pid or 'none'}); Windows {com} & TCP {port} released.",
    }


def test_tcp_link(tcp_url: str, probe_elm: bool = True) -> Dict[str, Any]:
    host, port = obd_can._validate_elm_tcp_target(tcp_url)
    if not probe_elm:
        if not _probe_tcp_open(host, port, timeout=1.2):
            return {
                "status": "error",
                "reachable": False,
                "tcp_url": f"tcp://{host}:{port}",
                "message": f"Could not reach tcp://{host}:{port}. Start the Windows BT-OBD bridge first and verify firewall / BindAddress.",
            }
        return {
            "status": "ok",
            "reachable": True,
            "tcp_url": f"tcp://{host}:{port}",
            "message": f"TCP bridge reachable at tcp://{host}:{port}.",
        }
    elm = obd_can.TcpSocketELM327(f"tcp://{host}:{port}", timeout=2.0)
    try:
        elm.open()
    except OSError:
        return {
            "status": "error",
            "reachable": False,
            "tcp_url": f"tcp://{host}:{port}",
            "message": f"Could not reach tcp://{host}:{port}. Start the Windows BT-OBD bridge first and verify firewall / BindAddress.",
        }
    try:
        try:
            elm.send("ATE0")
            ident = elm.send("ATI")
            volt_raw = elm.send("ATRV")
            proto = elm.send("ATDP")
        finally:
            elm.close()
        ident_clean = " ".join(ln.strip() for ln in ident.splitlines() if ln.strip() and ln.strip().upper() != "ATI")
        volt_clean = " ".join(ln.strip() for ln in volt_raw.splitlines() if ln.strip() and ln.strip().upper() != "ATRV")
        proto_clean = " ".join(ln.strip() for ln in proto.splitlines() if ln.strip() and ln.strip().upper() != "ATDP")
        elm_ok = bool(ident_clean or volt_clean)
        msg = (
            f"Bridge & ELM327 online! Adapter: {ident_clean[:60] or 'OK'} · Voltage: {volt_clean[:20] or '—'} · Protocol: {proto_clean[:40] or 'AUTO'}"
            if elm_ok
            else f"TCP bridge is reachable at tcp://{host}:{port}, but ELM327 did not reply on COM port. Verify Windows Bridge v2.6.4 is running, Carly is plugged in, ignition is ON, and phone Bluetooth is off."
        )
        return {
            "status": "ok" if elm_ok else "error",
            "reachable": True,
            "elm_ready": elm_ok,
            "tcp_url": f"tcp://{host}:{port}",
            "adapter_id": ident_clean[:120],
            "battery_raw": volt_clean[:40],
            "protocol": proto_clean[:80],
            "message": msg,
        }
    except Exception as exc:
        return {
            "status": "ok",
            "reachable": True,
            "elm_ready": False,
            "tcp_url": f"tcp://{host}:{port}",
            "message": f"TCP bridge is listening at tcp://{host}:{port}, but ELM327 handshake did not complete ({exc}). Verify Carly is plugged into the OBD-II port, ignition is Key-On, and the COM port is the Outgoing Bluetooth port.",
        }
