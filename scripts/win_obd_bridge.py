#!/usr/bin/env python3
"""
scripts/win_obd_bridge.py — Windows Bluetooth COM-Port to TCP Bridge for WSL2 & Rancher Desktop (v2.6.3).

Persistent-COM Architecture:
  Opens the Windows Outgoing Bluetooth COM port (e.g. COM6) ONCE and keeps the RFCOMM link warm
  across stateless Shop Bench HTTP/TCP requests so live telemetry streams smoothly without
  connect/disconnect thrashing. Closes COM cleanly on Ctrl+C or stop-flag.

Usage on Windows (PowerShell / CMD):
  python win_obd_bridge.py --com COM6 --port 35000 --bind 0.0.0.0
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, Tuple

COM_RE = re.compile(r"^COM([1-9][0-9]{0,2})$", re.IGNORECASE)


def open_com_port(com_name: str, baud: int) -> Tuple[str, Any]:
    try:
        import serial  # type: ignore
        # Keep DTR/RTS at False defaults: forcing DTR/RTS=True deadlocks Windows Bluetooth SPP on Carly adapters
        ser = serial.Serial(com_name, baudrate=baud, timeout=0.05, write_timeout=1.5)
        return ("pyserial", ser)
    except ImportError:
        path = com_name if com_name.startswith("\\\\.\\") else f"\\\\.\\{com_name}"
        f = open(path, "r+b", buffering=0)
        return ("raw", f)


class PersistentComHandle:
    def __init__(self, com_name: str, baud: int):
        self.com_name = com_name
        self.baud = baud
        self.kind: Optional[str] = None
        self.dev: Any = None

    def ensure_open(self) -> Tuple[str, Any]:
        if self.dev is not None:
            try:
                if self.kind == "pyserial" and not self.dev.is_open:
                    self.close()
                else:
                    if self.kind == "pyserial":
                        self.dev.reset_input_buffer()
                    return (self.kind or "raw", self.dev)
            except Exception:
                self.close()
        print(f"[i] Opening persistent Bluetooth link on {self.com_name} @ {self.baud} baud...")
        self.kind, self.dev = open_com_port(self.com_name, self.baud)
        self._warm_init()
        print(f"[OK] Persistent {self.com_name} link ACTIVE (keeping COM port open across polls).")
        return (self.kind, self.dev)

    def _warm_init(self) -> None:
        if self.kind != "pyserial" or self.dev is None:
            return
        for cmd, wait_s in (("ATZ", 2.2), ("ATE0", 0.6), ("ATL0", 0.6), ("ATS1", 0.6), ("ATSP0", 0.8), ("ATAT2", 0.6), ("0100", 6.0)):
            try:
                self.dev.reset_input_buffer()
                self.dev.write((cmd + "\r").encode("ascii"))
                deadline = time.time() + wait_s
                buf = bytearray()
                while time.time() < deadline:
                    chunk = self.dev.read(256)
                    if chunk:
                        buf.extend(chunk)
                        if b">" in buf:
                            break
            except Exception:
                break

    def drain(self) -> None:
        if self.dev is not None and self.kind == "pyserial":
            try:
                self.dev.reset_input_buffer()
            except Exception:
                self.close()

    def close(self) -> None:
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None
            self.kind = None


def bridge_client(conn: socket.socket, handle: PersistentComHandle, stop_flag: str = "") -> None:
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    try:
        kind, dev = handle.ensure_open()
    except Exception as e:
        print(f"[!] Could not open {handle.com_name}: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return

    stop_ev = threading.Event()
    serial_err = threading.Event()

    def _com_to_tcp():
        try:
            while not stop_ev.is_set():
                if stop_flag and os.path.exists(stop_flag):
                    stop_ev.set()
                    break
                data = dev.read(256)
                if data:
                    conn.sendall(data)
                else:
                    time.sleep(0.005)
        except OSError:
            if not stop_ev.is_set():
                serial_err.set()
            stop_ev.set()
        except Exception:
            stop_ev.set()

    t = threading.Thread(target=_com_to_tcp, daemon=True)
    t.start()
    try:
        conn.settimeout(0.1)
        while not stop_ev.is_set():
            if stop_flag and os.path.exists(stop_flag):
                break
            try:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                dev.write(chunk)
                if kind == "raw":
                    dev.flush()
            except socket.timeout:
                continue
    except OSError as e:
        print(f"[!] Bridge session I/O notice: {e}")
    finally:
        stop_ev.set()
        t.join(timeout=0.3)
        try:
            conn.close()
        except Exception:
            pass
        if serial_err.is_set():
            print(f"[!] Resetting {handle.com_name} after serial error.")
            handle.close()
        else:
            handle.drain()


def main() -> int:
    ap = argparse.ArgumentParser(description="Windows Bluetooth COM -> TCP ELM327 Bridge for Blue Lodge Shop Bench")
    ap.add_argument("--com", default="COM5", help="Windows Outgoing Bluetooth COM port (default: COM5)")
    ap.add_argument("--baud", type=int, default=38400, help="Baud rate (default: 38400)")
    ap.add_argument("--bind", default="127.0.0.1", help="Bind IP: 127.0.0.1, 0.0.0.0, or private LAN/Tailscale IPv4 (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=35000, help="TCP port (default: 35000)")
    ap.add_argument("--pid-file", default="", help="Optional path to write bridge PID")
    ap.add_argument("--stop-flag", default="", help="Optional path to a stop-flag file for clean shutdown")
    args = ap.parse_args()

    try:
        ip_obj = ipaddress.IPv4Address(args.bind.strip())
        if not (ip_obj.is_loopback or ip_obj.is_unspecified or ip_obj.is_private or ip_obj in ipaddress.IPv4Network("100.64.0.0/10")):
            raise ValueError("public IP")
    except ValueError:
        print(f"[!] Bind IP must be 127.0.0.1, 0.0.0.0, or private LAN/Tailscale IPv4 (got '{args.bind}').", file=sys.stderr)
        return 2

    com_clean = args.com.strip().upper()
    if not COM_RE.match(com_clean):
        print(f"[!] Invalid COM port '{args.com}'. Expected COM1..COM999 (e.g. COM5).", file=sys.stderr)
        return 2
    if not (1024 <= args.port <= 65535):
        print(f"[!] TCP port must be 1024..65535 (got {args.port}).", file=sys.stderr)
        return 2

    if args.stop_flag and os.path.exists(args.stop_flag):
        try:
            os.remove(args.stop_flag)
        except OSError:
            pass
    if args.pid_file:
        Path(args.pid_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.pid_file).write_text(str(os.getpid()), encoding="ascii")

    handle = PersistentComHandle(com_clean, args.baud)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(4)
    srv.settimeout(0.15)
    print(f"[OK] Shop Bench Windows BT-OBD Bridge v2.6.3 (PID {os.getpid()}) listening on tcp://{args.bind}:{args.port} -> {com_clean}")
    try:
        handle.ensure_open()
    except Exception as e:
        print(f"[!] {com_clean} not ready yet ({e}); will auto-connect on first Shop Bench request.")

    last_log = 0.0
    polls = 0
    try:
        while True:
            if args.stop_flag and os.path.exists(args.stop_flag):
                print("[i] Stop flag detected; shutting down cleanly.")
                break
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            polls += 1
            now = time.time()
            if now - last_log >= 15.0:
                print(f"[+] Shop Bench active from {addr[0]}:{addr[1]} (poll #{polls}; {com_clean} held persistently open)")
                last_log = now
            bridge_client(conn, handle, stop_flag=args.stop_flag)
    except KeyboardInterrupt:
        print("\n[i] Bridge stopped.")
    finally:
        handle.close()
        srv.close()
        for p in (args.pid_file, args.stop_flag):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        print(f"[i] Bridge stopped cleanly; TCP port {args.port} and {com_clean} released.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
