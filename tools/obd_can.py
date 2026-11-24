#!/usr/bin/env python3
"""
tools/obd_can.py — Sovereign OBD-II, ELM327 / Carly BLE, UDS Read-Only, & CAN-Bus Diagnostic Engine.

Zero-dependency core using Python stdlib (POSIX termios serial, native Linux socket.AF_CAN,
struct, csv, json) with automatic optional upgrades when pyserial, bleak (Carly/OBDLink BLE),
python-OBD, or python-can are installed in .venv.

Safety Policy:
  - Read-only diagnostics by default (OBD Mode 01/02/03/07/09/0A & UDS 0x22/0x19).
  - Mode 04 (Clear DTCs) requires explicit confirm_phrase == "CLEAR_DTCS" and records a
    pre-clear snapshot first.
  - Blind ECU flashing / arbitrary UDS 0x2E/0x34/0x36 writes are strictly refused.
"""
from __future__ import annotations

import asyncio
import csv
import glob
import importlib.util
import io
import ipaddress
import json
import os
import re
import select
import socket
import struct
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pid_catalog

# Known BLE GATT UART profiles for Carly Universal Adapter, OBDLink CX, Vgate iCar Pro, Veepeak, ELM327 BLE
BLE_OBD_NAME_HINTS = ("carly", "obd", "elm327", "vgate", "veepeak", "vlink", "ios-vlink", "v-link", "bimmer", "konnwei")
BLE_GATT_PROFILES = [
    # Standard ELM327 / Carly / Vgate FFF0 service
    ("0000fff0-0000-1000-8000-00805f9b34fb", "0000fff2-0000-1000-8000-00805f9b34fb", "0000fff1-0000-1000-8000-00805f9b34fb"),
    # HM-10 / Carly BLE / generic FFE0 single-char UART
    ("0000ffe0-0000-1000-8000-00805f9b34fb", "0000ffe1-0000-1000-8000-00805f9b34fb", "0000ffe1-0000-1000-8000-00805f9b34fb"),
    # OBDLink CX / 18F0 profile
    ("000018f0-0000-1000-8000-00805f9b34fb", "00002af1-0000-1000-8000-00805f9b34fb", "00002af0-0000-1000-8000-00805f9b34fb"),
    # Nordic UART Service (NUS)
    ("6e400001-b5a3-f393-e0a9-e50e24dcca9e", "6e400002-b5a3-f393-e0a9-e50e24dcca9e", "6e400003-b5a3-f393-e0a9-e50e24dcca9e"),
]

# Comprehensive built-in SAE J2012 DTC knowledge base (P/B/C/U codes)
SAE_DTC_DB: Dict[str, Dict[str, Any]] = {
    "P0010": {"title": "Intake Camshaft Position Actuator Circuit (Bank 1)", "system": "VVT", "severity": "medium", "causes": ["Oil control solenoid", "Low/dirty oil", "Connector/wiring open"]},
    "P0011": {"title": "Camshaft Position Timing Over-Advanced (Bank 1)", "system": "VVT", "severity": "high", "causes": ["Sludge-clogged VVT solenoid", "Stuck cam phaser", "Low oil pressure"]},
    "P0012": {"title": "Camshaft Position Timing Over-Retarded (Bank 1)", "system": "VVT", "severity": "high", "causes": ["Sticking VVT actuator", "Stretched timing chain", "Low oil pressure"]},
    "P0016": {"title": "Crankshaft/Camshaft Position Correlation (Bank 1 Sensor A)", "system": "Timing", "severity": "critical", "causes": ["Jumped timing belt/chain", "Failed tensioner", "Tone ring or sensor fault"]},
    "P0017": {"title": "Crankshaft/Camshaft Position Correlation (Bank 1 Sensor B)", "system": "Timing", "severity": "critical", "causes": ["Timing chain stretch", "Exhaust phaser pin failure"]},
    "P0101": {"title": "Mass Air Flow (MAF) Sensor Circuit Range/Performance", "system": "Air/Fuel", "severity": "medium", "causes": ["Dirty MAF wire", "Unmetered intake leak after MAF", "Clogged air filter"]},
    "P0102": {"title": "Mass Air Flow (MAF) Sensor Circuit Low Input", "system": "Air/Fuel", "severity": "medium", "causes": ["Unplugged MAF", "Open 5V/12V supply", "Failed MAF sensor"]},
    "P0106": {"title": "MAP/BARO Pressure Circuit Range/Performance", "system": "Air/Fuel", "severity": "medium", "causes": ["Vacuum hose leak", "Carbon-fouled MAP sensor", "Wiring fault"]},
    "P0113": {"title": "Intake Air Temperature (IAT) Sensor 1 Circuit High", "system": "Sensors", "severity": "low", "causes": ["Open IAT harness", "Unplugged airbox sensor"]},
    "P0117": {"title": "Engine Coolant Temperature (ECT) Circuit Low", "system": "Cooling", "severity": "high", "causes": ["Shorted ECT sensor", "Damaged harness"]},
    "P0118": {"title": "Engine Coolant Temperature (ECT) Circuit High", "system": "Cooling", "severity": "high", "causes": ["Open ECT connector", "Failed coolant temp sensor"]},
    "P0121": {"title": "Throttle Position Sensor A Circuit Range/Performance", "system": "Throttle", "severity": "high", "causes": ["Dirty throttle body", "Worn TPS track", "Connector corrosion"]},
    "P0128": {"title": "Coolant Thermostat Below Regulating Temperature", "system": "Cooling", "severity": "medium", "causes": ["Stuck-open thermostat", "Low coolant level", "Drifting ECT sensor"]},
    "P0131": {"title": "O2 Sensor Circuit Low Voltage (Bank 1 Sensor 1)", "system": "Emissions", "severity": "medium", "causes": ["Exhaust leak before O2", "Lean condition", "Failed upstream O2"]},
    "P0133": {"title": "O2 Sensor Slow Response (Bank 1 Sensor 1)", "system": "Emissions", "severity": "medium", "causes": ["Aging upstream O2 sensor", "Exhaust manifold crack"]},
    "P0135": {"title": "O2 Sensor Heater Circuit Malfunction (Bank 1 Sensor 1)", "system": "Emissions", "severity": "medium", "causes": ["Open O2 heater element", "Blown O2 heater fuse"]},
    "P0171": {"title": "System Too Lean (Bank 1)", "system": "Air/Fuel", "severity": "high", "causes": ["Vacuum/intake boot leak", "Dirty MAF sensor", "Weak fuel pump / clogged filter", "PCV valve stuck open", "Exhaust leak upstream of B1S1"]},
    "P0172": {"title": "System Too Rich (Bank 1)", "system": "Air/Fuel", "severity": "high", "causes": ["Leaking fuel injector", "High fuel pressure regulator", "Purge valve stuck open", "Skewed MAF/ECT"]},
    "P0174": {"title": "System Too Lean (Bank 2)", "system": "Air/Fuel", "severity": "high", "causes": ["Shared intake/PCV vacuum leak (if paired with P0171)", "Bank 2 intake gasket leak", "Weak fuel delivery"]},
    "P0175": {"title": "System Too Rich (Bank 2)", "system": "Air/Fuel", "severity": "high", "causes": ["Leaking Bank 2 injector", "High fuel rail pressure", "Faulty B2S1 O2"]},
    "P0191": {"title": "Fuel Rail Pressure Sensor Range/Performance", "system": "Fuel", "severity": "high", "causes": ["Failing high-pressure fuel pump", "Clogged fuel filter", "FRP sensor drift"]},
    "P0201": {"title": "Injector Circuit Open - Cylinder 1", "system": "Fuel", "severity": "high", "causes": ["Open injector coil", "Rodent-chewed injector pigtail", "ECU driver fault"]},
    "P0217": {"title": "Engine Coolant Over Temperature Condition", "system": "Cooling", "severity": "critical", "causes": ["Low coolant / blown hose", "Stuck-closed thermostat", "Failed water pump or cooling fan", "Blown head gasket"]},
    "P0299": {"title": "Turbocharger/Supercharger Underboost Condition", "system": "Forced Induction", "severity": "high", "causes": ["Boost hose / intercooler leak", "Sticking wastegate or diverter valve", "Failed boost solenoid"]},
    "P0300": {"title": "Random/Multiple Cylinder Misfire Detected", "system": "Ignition/Fuel", "severity": "critical", "causes": ["Vacuum leak", "Low fuel pressure", "Worn spark plugs", "Timing issue", "Restricted exhaust/cat"]},
    "P0301": {"title": "Cylinder 1 Misfire Detected", "system": "Ignition/Fuel", "severity": "high", "causes": ["Spark plug or ignition coil #1", "Fuel injector #1", "Low compression cyl #1"]},
    "P0302": {"title": "Cylinder 2 Misfire Detected", "system": "Ignition/Fuel", "severity": "high", "causes": ["Spark plug or ignition coil #2", "Fuel injector #2", "Low compression cyl #2"]},
    "P0303": {"title": "Cylinder 3 Misfire Detected", "system": "Ignition/Fuel", "severity": "high", "causes": ["Spark plug or ignition coil #3", "Fuel injector #3", "Low compression cyl #3"]},
    "P0304": {"title": "Cylinder 4 Misfire Detected", "system": "Ignition/Fuel", "severity": "high", "causes": ["Spark plug or ignition coil #4", "Fuel injector #4", "Low compression cyl #4"]},
    "P0305": {"title": "Cylinder 5 Misfire Detected", "system": "Ignition/Fuel", "severity": "high", "causes": ["Spark plug or ignition coil #5", "Fuel injector #5", "Low compression cyl #5"]},
    "P0306": {"title": "Cylinder 6 Misfire Detected", "system": "Ignition/Fuel", "severity": "high", "causes": ["Spark plug or ignition coil #6", "Fuel injector #6", "Low compression cyl #6"]},
    "P0307": {"title": "Cylinder 7 Misfire Detected", "system": "Ignition/Fuel", "severity": "high", "causes": ["Spark plug or ignition coil #7", "Fuel injector #7", "AFM/lifter collapse"]},
    "P0308": {"title": "Cylinder 8 Misfire Detected", "system": "Ignition/Fuel", "severity": "high", "causes": ["Spark plug or ignition coil #8", "Fuel injector #8", "Low compression cyl #8"]},
    "P0325": {"title": "Knock Sensor 1 Circuit (Bank 1)", "system": "Ignition", "severity": "medium", "causes": ["Failed knock sensor", "Corroded valley harness", "Improper sensor torque"]},
    "P0335": {"title": "Crankshaft Position Sensor A Circuit", "system": "Timing", "severity": "critical", "causes": ["Failed CKP sensor", "Damaged reluctor wheel", "Harness rub-through"]},
    "P0340": {"title": "Camshaft Position Sensor A Circuit (Bank 1)", "system": "Timing", "severity": "high", "causes": ["Failed CMP sensor", "Wiring open/short", "Timing misalignment"]},
    "P0401": {"title": "Exhaust Gas Recirculation (EGR) Flow Insufficient", "system": "Emissions", "severity": "medium", "causes": ["Carbon-clogged EGR passages", "Sticking EGR valve", "DPFE sensor"]},
    "P0420": {"title": "Catalyst System Efficiency Below Threshold (Bank 1)", "system": "Emissions", "severity": "medium", "causes": ["Degraded Bank 1 catalytic converter", "Exhaust leak between S1/S2", "Lazy downstream O2", "Prior misfire/oil burn"]},
    "P0430": {"title": "Catalyst System Efficiency Below Threshold (Bank 2)", "system": "Emissions", "severity": "medium", "causes": ["Degraded Bank 2 catalytic converter", "Exhaust leak", "Downstream O2"]},
    "P0440": {"title": "Evaporative Emission (EVAP) System Malfunction", "system": "EVAP", "severity": "low", "causes": ["Loose/worn gas cap", "Cracked EVAP vapor line", "Canister vent valve"]},
    "P0442": {"title": "EVAP System Small Leak Detected", "system": "EVAP", "severity": "low", "causes": ["Gas cap seal", "Purge/vent valve weeping", "Pin-hole vapor hose"]},
    "P0446": {"title": "EVAP Vent Control Circuit", "system": "EVAP", "severity": "low", "causes": ["Clogged charcoal canister vent solenoid", "Corroded rear harness"]},
    "P0455": {"title": "EVAP System Gross Leak Detected", "system": "EVAP", "severity": "low", "causes": ["Missing/loose gas cap", "Disconnected EVAP hose", "Stuck-open vent valve"]},
    "P0456": {"title": "EVAP System Very Small Leak Detected (0.020 in)", "system": "EVAP", "severity": "low", "causes": ["Aged gas cap O-ring", "Filler neck rust", "ESIM/NVLD leak pump"]},
    "P0505": {"title": "Idle Air Control System Malfunction", "system": "Air/Fuel", "severity": "medium", "causes": ["Carbon buildup in throttle bore", "Vacuum leak", "IAC valve"]},
    "P0507": {"title": "Idle Control System RPM Higher Than Expected", "system": "Air/Fuel", "severity": "medium", "causes": ["Vacuum leak", "Throttle body relearn needed", "PCV leak"]},
    "P0562": {"title": "System Voltage Low", "system": "Charging", "severity": "high", "causes": ["Weak 12V battery", "Failing alternator", "Corroded ground strap or terminal"]},
    "P0563": {"title": "System Voltage High", "system": "Charging", "severity": "high", "causes": ["Failed alternator voltage regulator", "Battery sense wire open"]},
    "P0606": {"title": "ECM/PCM Processor Fault", "system": "ECU", "severity": "critical", "causes": ["Low voltage brownout", "Poor ECU ground", "Internal PCM failure"]},
    "P0700": {"title": "Transmission Control System Malfunction (MIL Request)", "system": "Transmission", "severity": "high", "causes": ["Read TCM directly for specific shift/solenoid codes", "Low ATF"]},
    "P0715": {"title": "Input/Turbine Speed Sensor Circuit", "system": "Transmission", "severity": "high", "causes": ["Failed ISS sensor", "Metallic debris on sensor", "TCM harness"]},
    "P0730": {"title": "Incorrect Gear Ratio", "system": "Transmission", "severity": "critical", "causes": ["Low/burnt transmission fluid", "Worn clutch packs", "Shift solenoid"]},
    "P0741": {"title": "Torque Converter Clutch (TCC) Stuck Off", "system": "Transmission", "severity": "high", "causes": ["TCC solenoid", "Valve body wear", "Degraded ATF"]},
    "P2135": {"title": "TPS/Pedal Position Sensor A/B Voltage Correlation", "system": "Throttle", "severity": "high", "causes": ["Electronic throttle body failure", "Connector pin fretting"]},
    "P2195": {"title": "O2 Sensor Signal Biased/Stuck Lean (Bank 1 Sensor 1)", "system": "Air/Fuel", "severity": "high", "causes": ["Large vacuum leak", "Failed wideband A/F sensor", "Low fuel pressure"]},
    "C0035": {"title": "Left Front Wheel Speed Sensor Circuit", "system": "ABS/Chassis", "severity": "high", "causes": ["Worn wheel hub bearing", "Broken ABS sensor wire", "Rust jacking"]},
    "C0040": {"title": "Right Front Wheel Speed Sensor Circuit", "system": "ABS/Chassis", "severity": "high", "causes": ["Wheel speed sensor", "Damaged tone ring", "Harness chafing"]},
    "B0001": {"title": "Driver Frontal Stage 1 Deployment Control", "system": "SRS/Body", "severity": "critical", "causes": ["Clockspring ribbon open", "Under-seat yellow SRS connector"]},
    "U0001": {"title": "High Speed CAN Communication Bus Fault", "system": "CAN-Bus", "severity": "critical", "causes": ["CAN-H / CAN-L short to ground/power or shorted together", "Missing 120Ω bus termination (expect ~60Ω key-off)", "Water-damaged module pulling bus down"]},
    "U0073": {"title": "Control Module Communication Bus A Off", "system": "CAN-Bus", "severity": "critical", "causes": ["Bus-off error frames from failing node", "Bad splice pack / gateway corrosion"]},
    "U0100": {"title": "Lost Communication With ECM/PCM A", "system": "CAN-Bus", "severity": "critical", "causes": ["Blown ECM power/ignition fuse", "Bad ECM ground", "Open CAN-H/CAN-L twisted pair to ECM"]},
    "U0101": {"title": "Lost Communication With TCM", "system": "CAN-Bus", "severity": "high", "causes": ["TCM power/ground loss", "CAN wiring open near transmission harness"]},
    "U0121": {"title": "Lost Communication With ABS Control Module", "system": "CAN-Bus", "severity": "high", "causes": ["ABS fuse/ground", "Corroded ABS module connector", "CAN splice"]},
    "U0140": {"title": "Lost Communication With Body Control Module (BCM)", "system": "CAN-Bus", "severity": "high", "causes": ["Low 12V battery", "Water intrusion in footwell BCM", "CAN bus fault"]},
    "U0155": {"title": "Lost Communication With Instrument Panel Cluster (IPC)", "system": "CAN-Bus", "severity": "medium", "causes": ["Cluster power/ground", "Cold solder joints", "CAN gateway"]},
    "U0401": {"title": "Invalid Data Received From ECM/PCM", "system": "CAN-Bus", "severity": "medium", "causes": ["Secondary code set because ECM has an active powertrain fault — fix ECM P-codes first"]},
}


def lookup_dtc(code: str) -> Dict[str, Any]:
    """Decode any SAE J2012 OBD-II trouble code (P/B/C/U) using built-in KB + structural decode."""
    c = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    if len(c) != 5 or c[0] not in ("P", "B", "C", "U"):
        return {"code": c, "valid": False, "title": "Invalid SAE J2012 DTC format", "system": "Unknown", "severity": "unknown", "causes": []}

    known = SAE_DTC_DB.get(c)
    if known:
        return {"code": c, "valid": True, **known}

    prefix_map = {"P": "Powertrain", "B": "Body", "C": "Chassis", "U": "Network / CAN-Bus"}
    origin = "SAE Generic" if c[1] in ("0", "2") else "Manufacturer-Specific (OEM)"
    subsys_p = {
        "0": "Fuel & Air Metering / Auxiliary Emissions",
        "1": "Fuel & Air Metering",
        "2": "Fuel & Air Metering (Injector Circuit)",
        "3": "Ignition System or Misfire",
        "4": "Auxiliary Emission Controls",
        "5": "Vehicle Speed, Idle Control & Auxiliary Inputs",
        "6": "Computer & Auxiliary Outputs",
        "7": "Transmission",
        "8": "Transmission",
    }
    sys_name = subsys_p.get(c[2], prefix_map[c[0]]) if c[0] == "P" else prefix_map[c[0]]
    return {
        "code": c,
        "valid": True,
        "title": f"{origin} {prefix_map[c[0]]} Fault ({c})",
        "system": sys_name,
        "severity": "high" if c[0] == "U" else "medium",
        "causes": [f"Inspect OEM service manual for {c}", "Check connector power/ground and live PIDs before replacing parts"],
    }


def decode_dtc_bytes(b1: int, b2: int) -> Optional[str]:
    """Decode 2 raw ISO 15031-6 DTC bytes into a 5-char SAE J2012 code (e.g. 0x01, 0x71 -> P0171)."""
    if b1 == 0 and b2 == 0:
        return None
    prefixes = ("P", "C", "B", "U")
    p = prefixes[(b1 >> 6) & 0x03]
    d1 = (b1 >> 4) & 0x03
    d2 = b1 & 0x0F
    d3 = (b2 >> 4) & 0x0F
    d4 = b2 & 0x0F
    return f"{p}{d1}{d2:X}{d3:X}{d4:X}"


def decode_mode01_pid(pid_hex: str, data_bytes: List[int]) -> Optional[Dict[str, Any]]:
    """Decode standard SAE J1979 Mode 01 PID bytes into engineering units."""
    p = pid_hex.upper().zfill(2)
    a = data_bytes[0] if len(data_bytes) > 0 else 0
    b = data_bytes[1] if len(data_bytes) > 1 else 0
    if p == "04" and len(data_bytes) >= 1:
        return {"key": "engine_load_pct", "label": "Engine Load", "value": round(a * 100.0 / 255.0, 1), "unit": "%"}
    if p == "05" and len(data_bytes) >= 1:
        c = a - 40
        return {"key": "coolant_temp_c", "label": "Coolant Temp", "value": c, "value_f": round(c * 9 / 5 + 32, 1), "unit": "°C"}
    if p == "06" and len(data_bytes) >= 1:
        return {"key": "stft_b1_pct", "label": "STFT Bank 1", "value": round((a - 128) * 100.0 / 128.0, 1), "unit": "%"}
    if p == "07" and len(data_bytes) >= 1:
        return {"key": "ltft_b1_pct", "label": "LTFT Bank 1", "value": round((a - 128) * 100.0 / 128.0, 1), "unit": "%"}
    if p == "08" and len(data_bytes) >= 1:
        return {"key": "stft_b2_pct", "label": "STFT Bank 2", "value": round((a - 128) * 100.0 / 128.0, 1), "unit": "%"}
    if p == "09" and len(data_bytes) >= 1:
        return {"key": "ltft_b2_pct", "label": "LTFT Bank 2", "value": round((a - 128) * 100.0 / 128.0, 1), "unit": "%"}
    if p == "0B" and len(data_bytes) >= 1:
        return {"key": "map_kpa", "label": "Intake MAP", "value": a, "unit": "kPa"}
    if p == "0C" and len(data_bytes) >= 2:
        return {"key": "rpm", "label": "Engine RPM", "value": round(((a * 256) + b) / 4.0, 1), "unit": "rpm"}
    if p == "0D" and len(data_bytes) >= 1:
        return {"key": "speed_kmh", "label": "Vehicle Speed", "value": a, "value_mph": round(a * 0.621371, 1), "unit": "km/h"}
    if p == "0E" and len(data_bytes) >= 1:
        return {"key": "timing_adv_deg", "label": "Timing Advance", "value": round(a / 2.0 - 64.0, 1), "unit": "°"}
    if p == "0F" and len(data_bytes) >= 1:
        return {"key": "iat_c", "label": "Intake Air Temp", "value": a - 40, "unit": "°C"}
    if p == "10" and len(data_bytes) >= 2:
        return {"key": "maf_gps", "label": "MAF Air Flow", "value": round(((a * 256) + b) / 100.0, 2), "unit": "g/s"}
    if p == "11" and len(data_bytes) >= 1:
        return {"key": "throttle_pct", "label": "Throttle Pos", "value": round(a * 100.0 / 255.0, 1), "unit": "%"}
    if p == "14" and len(data_bytes) >= 1:
        return {"key": "o2_b1s1_v", "label": "O2 Bank 1 Sensor 1", "value": round(a / 200.0, 3), "unit": "V"}
    if p == "15" and len(data_bytes) >= 1:
        return {"key": "o2_b1s2_v", "label": "O2 Bank 1 Sensor 2", "value": round(a / 200.0, 3), "unit": "V"}
    if p == "42" and len(data_bytes) >= 2:
        return {"key": "ecu_voltage_v", "label": "Control Module Voltage", "value": round(((a * 256) + b) / 1000.0, 2), "unit": "V"}
    return None


def _check_optional_drivers() -> Dict[str, bool]:
    out = {
        "native_posix_serial": os.name == "posix",
        "native_linux_af_can": hasattr(socket, "AF_CAN"),
        "pyserial": False,
        "bleak": False,
        "python_obd": False,
        "python_can": False,
    }
    for mod, key in (("serial", "pyserial"), ("bleak", "bleak"), ("obd", "python_obd"), ("can", "python_can")):
        try:
            out[key] = importlib.util.find_spec(mod) is not None
        except Exception:
            out[key] = False
    return out


async def _scan_ble_adapters(timeout_sec: float = 4.0) -> List[Dict[str, Any]]:
    import bleak  # type: ignore
    devices = await bleak.BleakScanner.discover(timeout=timeout_sec)
    found = []
    for d in devices:
        nm = str(d.name or "")
        low = nm.lower()
        is_obd = any(h in low for h in BLE_OBD_NAME_HINTS)
        if is_obd:
            found.append({
                "transport": "ble",
                "name": nm or "Unknown BLE OBD",
                "address": d.address,
                "carly_compatible": "carly" in low or "obd" in low or "vgate" in low,
                "notes": "BLE GATT ELM327/Carly adapter detected via BlueZ/bleak",
            })
    return found


def discover_adapters(scan_ble: bool = True, ble_timeout: float = 3.5) -> Dict[str, Any]:
    """Auto-discover USB ELM327, Bluetooth RFCOMM, BLE (Carly/OBDLink), and Linux SocketCAN interfaces."""
    drivers = _check_optional_drivers()
    serial_ports: List[Dict[str, Any]] = []
    for pat, kind in (
        ("/dev/ttyUSB*", "usb_serial"),
        ("/dev/ttyACM*", "usb_cdc"),
        ("/dev/rfcomm*", "bt_rfcomm"),
        ("/dev/serial/by-id/*", "usb_by_id"),
    ):
        for p in sorted(glob.glob(pat)):
            serial_ports.append({"transport": "serial", "kind": kind, "path": p, "readable": os.access(p, os.R_OK | os.W_OK)})

    can_ifaces: List[Dict[str, Any]] = []
    for p in sorted(glob.glob("/sys/class/net/*")):
        iface = os.path.basename(p)
        if re.match(r"^(v?can|slcan)\d+$", iface):
            oper = ""
            try:
                oper = Path(p, "operstate").read_text().strip()
            except Exception:
                pass
            can_ifaces.append({"transport": "socketcan", "interface": iface, "state": oper or "unknown"})

    ble_devices: List[Dict[str, Any]] = []
    ble_status = "bleak_not_installed"
    if scan_ble and drivers["bleak"]:
        try:
            ble_devices = asyncio.run(_scan_ble_adapters(timeout_sec=ble_timeout))
            ble_status = "ok"
        except Exception as e:
            ble_status = f"ble_scan_unavailable: {e}"

    is_wsl = "microsoft" in (os.uname().release.lower() if hasattr(os, "uname") else "")
    guidance = [
        "Bench Simulator ('sim') is always available offline to test live PIDs, DTCs, freeze-frame, and CAN decoding.",
        "WSL2 / Rancher Desktop Laptop (Carly Classic BT that works with Torque Pro): Pair Carly in Windows Bluetooth Settings -> note Outgoing COM port (e.g. COM5) -> run `scripts\\win_obd_bridge.ps1 -ComPort COM5 -TcpPort 35000` on Windows -> select 'Windows BT-Bridge / Wi-Fi TCP' and connect to `tcp://host.docker.internal:35000` (Rancher) or `tcp://127.0.0.1:35000` (WSL2 mirrored).",
        "Native Linux Classic BT ELM327 / Carly SPP: Pair via `bluetoothctl`, run `sudo rfcomm bind 0 <MAC> 1`, select `/dev/rfcomm0`.",
        "BLE Adapters (Carly Universal BLE / OBDLink CX): Uses BLE GATT UART (`pip install bleak` + BlueZ); click 'Probe BLE GATT' to fingerprint exact UUIDs.",
        "USB ELM327 / OBDLink EX / CANable: Native Linux shows `/dev/ttyUSB0` or `/dev/ttyACM0` (on WSL2 pass USB via `usbipd wsl attach --busid <BUSID>`).",
    ]
    if is_wsl:
        guidance.append("WSL2 Detected: Use Windows PowerShell `usbipd wsl attach --busid <BUSID>` to pass USB OBD-II or CANable dongles into Linux; WSL2 does not natively expose host Bluetooth without a USB BT dongle passed via usbipd.")

    return {
        "status": "ok",
        "is_wsl": is_wsl,
        "drivers": drivers,
        "serial_ports": serial_ports,
        "can_interfaces": can_ifaces,
        "ble_status": ble_status,
        "ble_devices": ble_devices,
        "carly_and_setup_guide": guidance,
    }


class PosixSerialELM327:
    """Pure-stdlib POSIX termios serial ELM327 client (falls back cleanly to pyserial if present)."""

    def __init__(self, port: str, baud: int = 38400, timeout: float = 2.5):
        if not re.match(r"^/dev/(ttyUSB|ttyACM|rfcomm|pts/)[0-9A-Za-z._-]+$|^/dev/serial/by-id/[0-9A-Za-z._-]+$", port):
            raise ValueError(f"Invalid or disallowed serial device path: {port}")
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._fd: Optional[int] = None
        self._ser: Any = None

    def open(self) -> None:
        try:
            import serial  # type: ignore
            self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
            return
        except ImportError:
            pass
        import termios
        self._fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(self._fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        baud_const = getattr(termios, f"B{self.baud}", termios.B38400)
        attrs[4] = baud_const
        attrs[5] = baud_const
        termios.tcsetattr(self._fd, termios.TCSANOW, attrs)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None

    def cmd(self, cmd: str) -> str:
        return self.send(cmd)

    def send(self, cmd: str) -> str:
        wire = (cmd.strip() + "\r").encode("ascii")
        if self._ser is not None:
            self._ser.reset_input_buffer()
            self._ser.write(wire)
            buf = bytearray()
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                chunk = self._ser.read(256)
                if chunk:
                    buf.extend(chunk)
                    if b">" in buf:
                        break
            return buf.decode("ascii", errors="ignore").replace(">", "").strip()

        assert self._fd is not None
        os.write(self._fd, wire)
        buf = bytearray()
        c_up = cmd.strip().upper()
        base_to = max(self.timeout, 6.0) if c_up in ("0100",) else (max(self.timeout, 3.0) if c_up in ("ATZ", "0902", "ATSP0") else self.timeout)
        deadline = time.time() + base_to
        extended = False
        while time.time() < deadline:
            r, _, _ = select.select([self._fd], [], [], max(0.05, deadline - time.time()))
            if not r:
                continue
            chunk = os.read(self._fd, 512)
            if chunk:
                buf.extend(chunk)
                if b">" in buf:
                    break
                if not extended and (b"SEARCHING" in buf.upper() or b"BUS INIT" in buf.upper()):
                    deadline = max(deadline, time.time() + 5.5)
                    extended = True
        return buf.decode("ascii", errors="ignore").replace(">", "").strip()


async def _ble_elm327_exchange(address: str, commands: List[str], timeout: float = 3.0) -> Dict[str, str]:
    import bleak  # type: ignore
    results: Dict[str, str] = {}
    async with bleak.BleakClient(address, timeout=10.0) as client:
        services = client.services
        write_uuid = None
        notify_uuid = None
        for svc_u, w_u, n_u in BLE_GATT_PROFILES:
            svc = services.get_service(svc_u)
            if svc:
                write_uuid, notify_uuid = w_u, n_u
                break
        if not write_uuid or not notify_uuid:
            raise RuntimeError("BLE device does not expose a recognized ELM327/Carly GATT UART service (FFF0/FFE0/18F0/NUS)")

        buf = bytearray()
        done_ev = asyncio.Event()

        def _on_notify(_sender: Any, data: bytearray) -> None:
            buf.extend(data)
            if b">" in buf:
                done_ev.set()

        await client.start_notify(notify_uuid, _on_notify)
        try:
            for cmd in commands:
                buf.clear()
                done_ev.clear()
                await client.write_gatt_char(write_uuid, (cmd.strip() + "\r").encode("ascii"), response=False)
                try:
                    await asyncio.wait_for(done_ev.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                results[cmd] = buf.decode("ascii", errors="ignore").replace(">", "").strip()
        finally:
            await client.stop_notify(notify_uuid)
    return results


_ELM_STATUS_NOISE = ("NO DATA", "UNABLE TO CONNECT", "BUS INIT", "SEARCHING", "STOPPED", "ERROR", "CAN ERROR", "?")


def _extract_hex_lines(raw: str) -> List[List[str]]:
    """Split an ELM327 response into per-line 2-char hex byte token lists (works for BOTH ATS1 spaced and ATS0 spaceless)."""
    out: List[List[str]] = []
    for ln in re.split(r"[\r\n]+", raw or ""):
        s = ln.strip().upper()
        if not s:
            continue
        if any(noise in s for noise in _ELM_STATUS_NOISE):
            continue
        # Strip optional ISO-TP multi-frame prefix like '0:' / '1:' / '2:'
        s_clean = re.sub(r"^[0-9A-F]:\s*", "", s)
        # Only accept lines that are purely hex + whitespace
        if not re.match(r"^[0-9A-F\s]+$", s_clean):
            continue
        hex_only = re.sub(r"\s+", "", s_clean)
        if len(hex_only) < 2 or (len(hex_only) % 2 != 0):
            continue
        out.append([hex_only[i : i + 2] for i in range(0, len(hex_only), 2)])
    return out


def _parse_hex_response_bytes(raw: str, expected_mode: str, expected_pid: str = "") -> List[int]:
    """Extract payload bytes from an ELM327 ASCII hex response (handles ATS1 spaced, ATS0 spaceless, and multi-ECU lines)."""
    em = expected_mode.upper().zfill(2)
    ep = expected_pid.upper().zfill(2) if expected_pid else ""
    for tokens in _extract_hex_lines(raw):
        for i in range(len(tokens)):
            if tokens[i] == em:
                if ep:
                    if i + 1 < len(tokens) and tokens[i + 1] == ep:
                        return [int(x, 16) for x in tokens[i + 2 :]]
                else:
                    return [int(x, 16) for x in tokens[i + 1 :]]
    return []


def _parse_all_hex_response_bytes(raw: str, expected_mode: str, expected_pid: str = "") -> List[List[int]]:
    """Extract payload byte lists from ALL responding ECUs in a multi-ECU ELM327 response."""
    em = expected_mode.upper().zfill(2)
    ep = expected_pid.upper().zfill(2) if expected_pid else ""
    results: List[List[int]] = []
    for tokens in _extract_hex_lines(raw):
        for i in range(len(tokens)):
            if tokens[i] == em:
                if ep:
                    if i + 1 < len(tokens) and tokens[i + 1] == ep:
                        results.append([int(x, 16) for x in tokens[i + 2 :]])
                        break
                else:
                    results.append([int(x, 16) for x in tokens[i + 1 :]])
                    break
    return results


def _parse_voltage(raw: str) -> float:
    """Parse ELM327 ATRV battery voltage (requires decimal point so 'ISO 15765' can never match as 15.0V)."""
    for m in re.finditer(r"(?<![0-9A-Za-z])(\d{1,2}\.\d+)\s*V?\b", raw or "", re.I):
        try:
            v = float(m.group(1))
            if 5.0 <= v <= 30.0:
                return round(v, 2)
        except ValueError:
            continue
    return 0.0


def decode_mode01_readiness(raw_bytes: List[int]) -> Dict[str, Any]:
    """Decode SAE J1979 Mode 01 PID 01 (4 bytes: MIL, DTC count, continuous & non-continuous readiness monitors)."""
    if len(raw_bytes) < 4:
        return {}
    a, b, c, d = raw_bytes[0], raw_bytes[1], raw_bytes[2], raw_bytes[3]
    mil_on = bool(a & 0x80)
    dtc_cnt = a & 0x7F
    compression_ign = bool(b & 0x08)

    def _cont(avail_bit: int, incomp_bit: int) -> str:
        if not (b & (1 << avail_bit)):
            return "N/A"
        return "NOT_READY" if (b & (1 << incomp_bit)) else "READY"

    def _noncont(bit: int) -> str:
        if not (c & (1 << bit)):
            return "N/A"
        return "NOT_READY" if (d & (1 << bit)) else "READY"

    if compression_ign:
        return {
            "mil_on": mil_on,
            "dtc_count": dtc_cnt,
            "ignition_type": "compression_diesel",
            "misfire": _cont(0, 4),
            "fuel": _cont(1, 5),
            "components": _cont(2, 6),
            "nmhc_cat": _noncont(0),
            "nox_scr": _noncont(1),
            "boost_pressure": _noncont(3),
            "exhaust_gas_sensor": _noncont(5),
            "pm_filter": _noncont(6),
            "egr_vvt": _noncont(7),
        }
    return {
        "mil_on": mil_on,
        "dtc_count": dtc_cnt,
        "ignition_type": "spark_gasoline",
        "misfire": _cont(0, 4),
        "fuel": _cont(1, 5),
        "components": _cont(2, 6),
        "catalyst": _noncont(0),
        "heated_catalyst": _noncont(1),
        "evap": _noncont(2),
        "secondary_air": _noncont(3),
        "o2_sensor": _noncont(5),
        "o2_heater": _noncont(6),
        "egr_vvt": _noncont(7),
    }


def probe_fast_frame_support(elm: Any) -> bool:
    """Auto-probe ELM327 '1' single-frame response hint (e.g. '010C1' -> 3-5x faster on Carly/OBDLink/STN; falls back safely on clones)."""
    try:
        resp = elm.send("010C1")
        if "?" in resp or "ERROR" in resp.upper():
            return False
        b = _parse_hex_response_bytes(resp, "41", "0C")
        return len(b) >= 2
    except Exception:
        return False


_SAFE_RO_CMD_RE = re.compile(r"^(?:01[0-9A-F]{2}|02[0-9A-F]{4}|03|06[0-9A-F]{2}|07|09[0-9A-F]{2}|0A|22[0-9A-F]{4}|AT(?:I|RV|DP|DPN))$")
_SAFE_ECU_HDR_RE = re.compile(r"^[0-9A-F]{3}$")


def read_only_ecu_query(
    transport: str = "sim",
    port_or_addr: str = "",
    commands: Optional[List[str]] = None,
    ecu_header: str = "",
    discover_ecus: bool = True,
    baud: int = 38400,
) -> Dict[str, Any]:
    """
    Curious, strictly read-only multi-ECU inspection tool for the Mechanic Copilot:
    - Discovers responding ECUs with ATH1 headers (e.g. 7E8 ECM, 7EA TCM, 7EB ABS/BCM)
    - Decodes Mode 01 PID 01 emissions readiness monitors + Mode 09 Cal-IDs / ECU names
    - Allows safe read-only PID/UDS 0x22 queries while strictly blocking all write/clear/coding modes.
    """
    t = str(transport or "sim").strip().lower()
    hdr = re.sub(r"[^0-9A-Fa-f]", "", str(ecu_header or "")).upper()
    if hdr and not _SAFE_ECU_HDR_RE.match(hdr):
        return {"status": "error", "message": f"Invalid CAN ECU header '{ecu_header}' (expected 3 hex chars like 7E0, 7E1, 7DF)."}

    clean_cmds: List[str] = []
    for raw_c in (commands or [])[:16]:
        c = re.sub(r"\s+", "", str(raw_c or "")).upper()
        if not c:
            continue
        if not _SAFE_RO_CMD_RE.match(c):
            return {
                "status": "blocked",
                "message": f"Command '{raw_c}' blocked by read-only safety policy. Allowed read-only modes: 01xx, 02xxxx, 03, 06xx, 07, 09xx, 0A, 22xxxx, ATI, ATRV, ATDP.",
            }
        clean_cmds.append(c)

    if t == "sim":
        return {
            "status": "ok",
            "transport": "sim",
            "ecus_discovered": [
                {"can_id": "7E8", "role": "ECM (Engine Control Module)", "sample": "7E8 06 41 00 BE 7F B8 13"},
                {"can_id": "7EA", "role": "TCM (Transmission Control Module)", "sample": "7EA 06 41 00 80 00 00 01"},
            ],
            "readiness": {"mil_on": False, "dtc_count": 0, "misfire": "READY", "fuel": "READY", "catalyst": "READY", "evap": "READY", "o2_sensor": "READY"},
            "calibration_ids": ["12663375-SIM"],
            "ecu_names": ["ECM-EngineControl (Simulated)"],
            "responses": {c: "SIM_OK" for c in clean_cmds},
        }

    if t not in ("tcp", "serial"):
        return {"status": "error", "message": "Multi-ECU read-only query supports tcp or serial transports."}

    elm: Any = TcpSocketELM327(port_or_addr, timeout=2.0) if t == "tcp" else PosixSerialELM327(port_or_addr, baud=int(baud), timeout=2.0)
    elm.open()
    ecus_found: Dict[str, Dict[str, Any]] = {}
    readiness: Dict[str, Any] = {}
    cal_ids: List[str] = []
    ecu_names: List[str] = []
    cmd_responses: Dict[str, Any] = {}

    ecu_role_hints = {
        "7E8": "ECM / Engine Control Module",
        "7E9": "TCM / Secondary Powertrain Module",
        "7EA": "TCM / Transmission Control Module",
        "7EB": "Chassis / ABS / Hybrid Module",
        "7EC": "Body / Auxiliary Control Module",
    }

    try:
        for init_c in ("ATE0", "ATL0", "ATS1", "ATAT2", "ATST14"):
            elm.send(init_c)
        if discover_ecus:
            elm.send("ATH1")
            try:
                r00 = elm.send("0100")
                for ln in re.split(r"[\r\n]+", r00 or ""):
                    m_h = re.match(r"^\s*([0-9A-Fa-f]{3})\s+(.+)$", ln.strip())
                    if m_h:
                        cid = m_h.group(1).upper()
                        ecus_found[cid] = {
                            "can_id": cid,
                            "role": ecu_role_hints.get(cid, "CAN ECU Module"),
                            "sample": ln.strip()[:80],
                        }
                r01 = elm.send("0101")
                for ln in re.split(r"[\r\n]+", r01 or ""):
                    b01 = _parse_hex_response_bytes(ln, "41", "01")
                    if len(b01) >= 4:
                        dec_r = decode_mode01_readiness(b01[:4])
                        if dec_r and (not readiness or dec_r.get("catalyst") != "N/A"):
                            readiness = dec_r
            finally:
                elm.send("ATH0")

            # Mode 09 PID 04 (Calibration IDs) & PID 0A (ECU Name)
            r04 = elm.send("0904")
            ascii04 = "".join(chr(x) for x in _parse_hex_response_bytes(r04, "49", "04") if 32 <= x <= 126)
            for tok in re.findall(r"[A-Za-z0-9_-]{4,20}", ascii04):
                if tok not in cal_ids:
                    cal_ids.append(tok)
            r0a = elm.send("090A")
            ascii0a = "".join(chr(x) for x in _parse_hex_response_bytes(r0a, "49", "0A") if 32 <= x <= 126).strip()
            if ascii0a:
                ecu_names.append(ascii0a[:80])

        if clean_cmds:
            if hdr:
                elm.send(f"ATSH{hdr}")
            try:
                for c in clean_cmds:
                    raw = elm.send(c)
                    entry: Dict[str, Any] = {"raw": raw[:300]}
                    if c.startswith("01") and len(c) == 4:
                        p = c[2:]
                        if p == "01":
                            entry["decoded"] = decode_mode01_readiness(_parse_hex_response_bytes(raw, "41", "01"))
                        else:
                            b = _parse_hex_response_bytes(raw, "41", p)
                            dec = pid_catalog.decode_standard_pid(p, b)
                            if dec:
                                entry["decoded"] = dec
                    elif c in ("03", "07", "0A"):
                        rm = {"03": "43", "07": "47", "0A": "4A"}[c]
                        entry["dtcs"] = _parse_dtc_response(raw, rm)
                    elif c == "0902":
                        entry["vin"] = _parse_vin_response(raw)
                    elif c == "ATRV":
                        entry["voltage_v"] = _parse_voltage(raw)
                    cmd_responses[c] = entry
            finally:
                if hdr:
                    elm.send("ATSH7DF")
    finally:
        try:
            elm.send("ATH0")
            elm.send("ATSH7DF")
        except Exception:
            pass
        elm.close()

    return {
        "status": "ok",
        "transport": t,
        "ecu_header": hdr or "7DF (broadcast)",
        "ecus_discovered": sorted(ecus_found.values(), key=lambda x: x["can_id"]),
        "readiness": readiness,
        "calibration_ids": cal_ids[:10],
        "ecu_names": ecu_names[:6],
        "responses": cmd_responses,
    }


def _parse_vin_response(raw: str) -> str:
    """Decode ISO-15765 multi-frame or single-frame Mode 09 PID 02 VIN response (spaced or spaceless)."""
    joined_bytes: List[int] = []
    started = False
    for ln in re.split(r"[\r\n]+", raw or ""):
        s = ln.strip().upper()
        if not s or any(noise in s for noise in _ELM_STATUS_NOISE):
            continue
        # Skip ISO-TP total-length header line (e.g. '014')
        if re.match(r"^[0-9A-F]{3}$", s):
            continue
        s_clean = re.sub(r"^[0-9A-F]:\s*", "", s)
        if not re.match(r"^[0-9A-F\s]+$", s_clean):
            continue
        hex_only = re.sub(r"\s+", "", s_clean)
        if len(hex_only) < 2 or (len(hex_only) % 2 != 0):
            continue
        toks = [hex_only[i : i + 2] for i in range(0, len(hex_only), 2)]
        if not started:
            for i in range(len(toks) - 1):
                if toks[i] == "49" and toks[i + 1] == "02":
                    rest = [int(x, 16) for x in toks[i + 2 :]]
                    # Drop leading message-count byte (0x01) if present
                    if rest and rest[0] == 1:
                        rest = rest[1:]
                    joined_bytes.extend(rest)
                    started = True
                    break
        else:
            # Stop if a second ECU starts a fresh 49 02 sequence
            if len(toks) >= 2 and toks[0] == "49" and toks[1] == "02":
                break
            joined_bytes.extend(int(x, 16) for x in toks)
    ascii_chars = "".join(chr(x) for x in joined_bytes if 32 <= x <= 126)
    m = re.search(r"[A-HJ-NPR-Z0-9]{17}", ascii_chars)
    return m.group(0) if m else ascii_chars.strip()[-17:]


def _parse_dtc_response(raw: str, resp_mode: str = "43") -> List[Dict[str, Any]]:
    """Decode DTCs across all responding ECUs (ECM, TCM, etc.), handling spaced, spaceless, and multi-ECU concatenated '43 00 43 00' zero-DTC responses."""
    found: List[Dict[str, Any]] = []
    seen = set()
    em = resp_mode.upper().zfill(2)
    for tokens in _extract_hex_lines(raw):
        i = 0
        while i < len(tokens):
            if tokens[i] != em:
                i += 1
                continue
            # Start of an ECU DTC frame: advance past resp_mode byte
            i += 1
            if i >= len(tokens):
                break
            # Find where a concatenated second ECU frame might begin (e.g. '43 00 43 00')
            first_b = int(tokens[i], 16)
            # ISO 15765-4 CAN: '43 00' means 0 DTCs from this ECU — do NOT read next ECU's '43 00' as C0300!
            if first_b == 0:
                i += 1
                # Also skip trailing 00 padding bytes belonging to this empty frame (e.g. '43 00 00 00')
                while i < len(tokens) and tokens[i] == "00":
                    i += 1
                continue
            # Collect payload bytes up to end of line or next embedded ECU frame
            rem_bytes = [int(x, 16) for x in tokens[i:]]
            # ISO 15765-4 CAN includes a 1-byte DTC count header when len is odd OR when count*2+1 <= len and count <= 20
            if len(rem_bytes) >= 1 and rem_bytes[0] <= 20 and len(rem_bytes) >= 1 + 2 * rem_bytes[0] and (
                len(rem_bytes) == 1 + 2 * rem_bytes[0]
                or (1 + 2 * rem_bytes[0] < len(tokens) - i and tokens[i + 1 + 2 * rem_bytes[0]] == em)
            ):
                cnt = rem_bytes[0]
                pair_bytes = rem_bytes[1 : 1 + 2 * cnt]
                i += 1 + 2 * cnt
            else:
                idx = 1 if (len(rem_bytes) % 2 == 1 and rem_bytes[0] <= 20) else 0
                pair_bytes = rem_bytes[idx:]
                i = len(tokens)
            for p_idx in range(0, len(pair_bytes) - 1, 2):
                code = decode_dtc_bytes(pair_bytes[p_idx], pair_bytes[p_idx + 1])
                if code and code not in seen:
                    seen.add(code)
                    found.append(lookup_dtc(code))
    return found


def _simulate_obd_scan(profile: str = "lean_misfire") -> Dict[str, Any]:
    """Realistic offline vehicle OBD-II bench simulator for testing & demos."""
    profiles = {
        "healthy": {
            "vin": "1GCVKREC0JZ000001",
            "protocol": "ISO 15765-4 (CAN 11-bit 500 kbaud) [SIM]",
            "battery_v": 14.2,
            "pids": {
                "rpm": {"label": "Engine RPM", "value": 680.0, "unit": "rpm"},
                "coolant_temp_c": {"label": "Coolant Temp", "value": 90, "value_f": 194.0, "unit": "°C"},
                "engine_load_pct": {"label": "Engine Load", "value": 21.6, "unit": "%"},
                "stft_b1_pct": {"label": "STFT Bank 1", "value": 0.8, "unit": "%"},
                "ltft_b1_pct": {"label": "LTFT Bank 1", "value": 1.6, "unit": "%"},
                "stft_b2_pct": {"label": "STFT Bank 2", "value": -0.8, "unit": "%"},
                "ltft_b2_pct": {"label": "LTFT Bank 2", "value": 0.8, "unit": "%"},
                "maf_gps": {"label": "MAF Air Flow", "value": 5.42, "unit": "g/s"},
                "map_kpa": {"label": "Intake MAP", "value": 33, "unit": "kPa"},
                "throttle_pct": {"label": "Throttle Pos", "value": 14.5, "unit": "%"},
                "timing_adv_deg": {"label": "Timing Advance", "value": 14.5, "unit": "°"},
                "iat_c": {"label": "Intake Air Temp", "value": 27, "unit": "°C"},
                "ecu_voltage_v": {"label": "Control Module Voltage", "value": 14.18, "unit": "V"},
            },
            "codes": [],
            "pending_codes": [],
            "readiness": {"mil_on": False, "misfire": "READY", "fuel": "READY", "catalyst": "READY", "evap": "READY", "o2_sensor": "READY"},
            "freeze_frame": {},
        },
        "lean_misfire": {
            "vin": "1GCVKREC0JZ000001",
            "protocol": "ISO 15765-4 (CAN 11-bit 500 kbaud) [SIM]",
            "battery_v": 13.9,
            "pids": {
                "rpm": {"label": "Engine RPM", "value": 745.0, "unit": "rpm"},
                "coolant_temp_c": {"label": "Coolant Temp", "value": 92, "value_f": 197.6, "unit": "°C"},
                "engine_load_pct": {"label": "Engine Load", "value": 29.4, "unit": "%"},
                "stft_b1_pct": {"label": "STFT Bank 1", "value": 11.7, "unit": "%"},
                "ltft_b1_pct": {"label": "LTFT Bank 1", "value": 18.8, "unit": "%"},
                "stft_b2_pct": {"label": "STFT Bank 2", "value": 2.3, "unit": "%"},
                "ltft_b2_pct": {"label": "LTFT Bank 2", "value": 3.1, "unit": "%"},
                "maf_gps": {"label": "MAF Air Flow", "value": 4.18, "unit": "g/s"},
                "map_kpa": {"label": "Intake MAP", "value": 44, "unit": "kPa"},
                "throttle_pct": {"label": "Throttle Pos", "value": 16.9, "unit": "%"},
                "timing_adv_deg": {"label": "Timing Advance", "value": 11.0, "unit": "°"},
                "iat_c": {"label": "Intake Air Temp", "value": 31, "unit": "°C"},
                "ecu_voltage_v": {"label": "Control Module Voltage", "value": 13.86, "unit": "V"},
            },
            "codes": ["P0171", "P0302"],
            "pending_codes": ["P0300"],
            "readiness": {"mil_on": True, "misfire": "NOT_READY", "fuel": "NOT_READY", "catalyst": "READY", "evap": "READY", "o2_sensor": "READY"},
            "freeze_frame": {"trigger_dtc": "P0171", "rpm": 720, "coolant_temp_c": 88, "engine_load_pct": 31.0, "stft_b1_pct": 14.1, "ltft_b1_pct": 19.5, "speed_kmh": 0, "maf_gps": 3.95},
        },
        "can_bus_fault": {
            "vin": "WBAPH5C55BA000002",
            "protocol": "ISO 15765-4 (CAN 11-bit 500 kbaud) [SIM]",
            "battery_v": 11.6,
            "pids": {
                "rpm": {"label": "Engine RPM", "value": 0.0, "unit": "rpm"},
                "coolant_temp_c": {"label": "Coolant Temp", "value": 24, "value_f": 75.2, "unit": "°C"},
                "ecu_voltage_v": {"label": "Control Module Voltage", "value": 11.54, "unit": "V"},
            },
            "codes": ["U0100", "U0121", "P0562"],
            "pending_codes": ["U0401"],
            "readiness": {"mil_on": True, "misfire": "READY", "fuel": "READY", "catalyst": "NOT_READY", "evap": "NOT_READY", "o2_sensor": "READY"},
            "freeze_frame": {"trigger_dtc": "U0100", "ecu_voltage_v": 10.9, "speed_kmh": 42, "rpm": 1950},
        },
    }
    p = profiles.get(profile, profiles["lean_misfire"])
    dtcs = [{**lookup_dtc(c), "state": "confirmed"} for c in p["codes"]]
    dtcs += [{**lookup_dtc(c), "state": "pending"} for c in p["pending_codes"]]
    return {
        "status": "ok",
        "adapter_type": "sim",
        "port_or_addr": f"sim://{profile}",
        "vin": p["vin"],
        "protocol": p["protocol"],
        "battery_v": p["battery_v"],
        "pids": p["pids"],
        "dtcs": dtcs,
        "freeze_frame": p["freeze_frame"],
        "readiness": p["readiness"],
        "uds_read_only_info": {
            "supported": True,
            "mode": "READ_ONLY (UDS 0x22 / 0x19)",
            "ecu_name": "ECM-EngineControl (Simulated)",
            "calibration_id": "12689421-SIM-A",
            "coding_note": "ECU coding/flashing writes are disabled by safety policy; read-only inspection active.",
        },
    }


def _validate_elm_tcp_target(addr: str) -> Tuple[str, int]:
    """SSRF-safe validation for Wi-Fi ELM327 or Windows BT-COM TCP bridge (localhost/host.docker.internal/RFC1918 only)."""
    raw = str(addr or "").strip()
    if "://" not in raw:
        raw = "tcp://" + raw
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").strip().lower()
    port = int(parsed.port or 35000)
    if not host or not (1 <= port <= 65535):
        raise ValueError(f"Invalid TCP ELM327 target: {addr}")
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal", "host.lima.internal"):
        return host, port
    try:
        ip = ipaddress.ip_address(host)
        if (ip.is_private or ip.is_loopback) and not ip.is_link_local and not ip.is_unspecified:
            return host, port
    except ValueError:
        pass
    raise ValueError(f"TCP ELM327 target '{host}' must be localhost, host.docker.internal, or a private LAN IP (e.g. 192.168.0.10:35000)")


class TcpSocketELM327:
    """Zero-dependency TCP ELM327 client for Windows Bluetooth-COM Bridge (WSL2 / Rancher) & Wi-Fi ELM327."""

    def __init__(self, target: str, timeout: float = 2.5):
        self.host, self.port = _validate_elm_tcp_target(target)
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=4.0)
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        self._sock.settimeout(self.timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _drain_pending(self) -> None:
        if self._sock is None:
            return
        while True:
            r, _, _ = select.select([self._sock], [], [], 0.0)
            if not r:
                break
            try:
                if not self._sock.recv(1024):
                    break
            except Exception:
                break

    def cmd(self, cmd: str) -> str:
        return self.send(cmd)

    def send(self, cmd: str) -> str:
        assert self._sock is not None
        self._drain_pending()
        c_up = cmd.strip().upper()
        self._sock.sendall((cmd.strip() + "\r").encode("ascii"))
        buf = bytearray()
        if c_up == "0100":
            eff_timeout = max(self.timeout, 6.0)
        elif c_up in ("ATZ", "0902", "ATSP0"):
            eff_timeout = max(self.timeout, 3.0)
        else:
            eff_timeout = self.timeout
        deadline = time.time() + eff_timeout
        extended = False
        while time.time() < deadline:
            r, _, _ = select.select([self._sock], [], [], max(0.05, deadline - time.time()))
            if not r:
                continue
            chunk = self._sock.recv(512)
            if not chunk:
                # Remote bridge closed socket (e.g. COM port open error / stall reset)
                if not buf:
                    raise ConnectionError(f"TCP bridge at {self.host}:{self.port} closed connection while executing '{cmd}'")
                break
            buf.extend(chunk)
            if b">" in buf:
                break
            # If ELM327 is auto-searching CAN protocol, wait up to 6s so we never desync the next command!
            if not extended and (b"SEARCHING" in buf.upper() or b"BUS INIT" in buf.upper()):
                deadline = max(deadline, time.time() + 5.5)
                extended = True
        return buf.decode("ascii", errors="ignore").replace(">", "").strip()

    def sniff_raw(self, duration_sec: float = 2.0) -> str:
        """Read passive ATMA/ATCRA lines for duration_sec then cleanly send CR to exit monitor mode."""
        assert self._sock is not None
        buf = bytearray()
        deadline = time.time() + min(10.0, max(0.5, float(duration_sec)))
        while time.time() < deadline:
            r, _, _ = select.select([self._sock], [], [], 0.1)
            if not r:
                continue
            chunk = self._sock.recv(1024)
            if not chunk:
                break
            buf.extend(chunk)
            if b"BUFFER FULL" in buf or b"STOPPED" in buf:
                break
        try:
            self._sock.sendall(b"\r")
            time.sleep(0.15)
            self._sock.setblocking(False)
            try:
                self._sock.recv(1024)
            except Exception:
                pass
            self._sock.settimeout(self.timeout)
        except Exception:
            pass
        return buf.decode("ascii", errors="ignore")


async def _ble_gatt_fingerprint(address: str = "", scan_timeout: float = 4.0) -> Dict[str, Any]:
    """1-click BLE GATT service & characteristic fingerprinter for Carly / OBDLink / Vgate adapters."""
    import bleak  # type: ignore
    devices = await bleak.BleakScanner.discover(timeout=scan_timeout)
    found_list = []
    target_addr = address.strip()
    for d in devices:
        nm = str(d.name or "")
        low = nm.lower()
        is_obd = any(h in low for h in BLE_OBD_NAME_HINTS) or (target_addr and d.address.lower() == target_addr.lower())
        if is_obd:
            found_list.append({"name": nm or "Unnamed BLE", "address": d.address})
    if not target_addr and found_list:
        target_addr = found_list[0]["address"]
    if not target_addr:
        return {"status": "ok", "matched_devices": [], "probed": None, "verdict": "No Carly/OBD BLE adapters detected nearby. If your Carly connects to Torque Pro, it uses Classic Bluetooth SPP (use Windows COM -> TCP bridge on WSL2/Rancher!)."}

    services_out = []
    recognized_uart = False
    known_svcs = {s[0] for s in BLE_GATT_PROFILES}
    async with bleak.BleakClient(target_addr, timeout=10.0) as client:
        for svc in client.services:
            su = str(svc.uuid).lower()
            if su in known_svcs:
                recognized_uart = True
            chars = [{"uuid": str(c.uuid).lower(), "properties": list(c.properties)} for c in svc.characteristics]
            services_out.append({"service_uuid": su, "description": svc.description, "characteristics": chars})

    verdict = (
        "OPEN_ELM327_BLE_UART: Recognized UART GATT service found — ready for direct BLE polling!"
        if recognized_uart
        else "PROPRIETARY_OR_CLASSIC_BT: No standard ELM327 BLE UART UUID found. If it works in Torque Pro, pair via Classic Bluetooth SPP (/dev/rfcomm0 or Windows COM -> TCP bridge)."
    )
    return {"status": "ok", "matched_devices": found_list, "probed_address": target_addr, "recognized_uart": recognized_uart, "verdict": verdict, "services": services_out}


def probe_ble_gatt(address: str = "", scan_timeout: float = 4.0) -> Dict[str, Any]:
    drivers = _check_optional_drivers()
    if not drivers["bleak"]:
        return {
            "status": "driver_missing",
            "message": "BLE GATT probe requires `bleak` in .venv (pip install bleak) + a Linux BlueZ adapter. Note: if your Carly adapter connects to Torque Pro, it uses Classic Bluetooth SPP — use our bundled Windows BT-COM TCP Bridge (scripts/win_obd_bridge.ps1) on WSL2 or Rancher!",
            "drivers": drivers,
        }
    try:
        return asyncio.run(_ble_gatt_fingerprint(address=address, scan_timeout=scan_timeout))
    except Exception as e:
        return {"status": "error", "message": f"BLE GATT probe failed: {e}"}


def _send_sequence_failfast(elm: Any, commands: List[str], max_consecutive_empty: int = 3) -> Dict[str, str]:
    out: Dict[str, str] = {}
    empty_streak = 0
    for c in commands:
        resp = elm.send(c)
        out[c] = resp
        if not resp.strip():
            empty_streak += 1
            if empty_streak >= max_consecutive_empty:
                raise TimeoutError(
                    f"ELM327 adapter silent for {empty_streak} consecutive commands (last: {c}). "
                    "Verify Windows BT-OBD Bridge is running v2.6.4, ignition is Key-On, and phone Bluetooth is off."
                )
        else:
            empty_streak = 0
    return out


def _run_elm_commands(transport: str, port_or_addr: str, commands: List[str], baud: int = 38400, timeout: float = 2.0) -> Dict[str, str]:
    t = str(transport or "sim").strip().lower()
    if t == "serial":
        if not port_or_addr:
            raise ValueError("Specify serial port (e.g. /dev/ttyUSB0 or /dev/rfcomm0).")
        elm = PosixSerialELM327(port_or_addr, baud=int(baud), timeout=timeout)
        elm.open()
        try:
            return _send_sequence_failfast(elm, commands)
        finally:
            elm.close()
    if t == "tcp":
        if not port_or_addr:
            raise ValueError("Specify TCP bridge address (e.g. tcp://host.docker.internal:35000 or tcp://192.168.0.10:35000).")
        elm = TcpSocketELM327(port_or_addr, timeout=timeout)
        elm.open()
        try:
            return _send_sequence_failfast(elm, commands)
        finally:
            elm.close()
    if t == "ble":
        if not port_or_addr:
            raise ValueError("Specify BLE MAC/UUID address from Scan Adapters.")
        if not _check_optional_drivers()["bleak"]:
            raise ValueError("BLE transport requires `bleak` in .venv + BlueZ.")
        return asyncio.run(_ble_elm327_exchange(port_or_addr, commands))
    raise ValueError(f"Unsupported ELM327 transport: {t}")


def _discover_supported_on_open_elm(elm: Any) -> List[str]:
    """Query SAE J1979 0100 -> 0120 -> 0140 -> 0160 -> 0180 bitmasks cleanly on an already-open ELM327 link."""
    supported: List[str] = []
    # Query 0100 first (waits up to 6s if ELM327 prints SEARCHING...)
    r00 = elm.send("0100")
    found_00: List[str] = []
    for b in _parse_all_hex_response_bytes(r00, "41", "00"):
        found_00.extend(pid_catalog.decode_supported_pid_bitmask("00", b[:4]))
    supported.extend(found_00)
    if not found_00:
        return []
    # Chain subsequent bitmask banks whenever supported (or check 0120/0140 safely)
    for bank_hex, next_flag in (("20", "20"), ("40", "40"), ("60", "60"), ("80", "80")):
        if next_flag in supported or bank_hex in ("20", "40"):
            rb = elm.send(f"01{bank_hex}")
            for b in _parse_all_hex_response_bytes(rb, "41", bank_hex):
                supported.extend(pid_catalog.decode_supported_pid_bitmask(bank_hex, b[:4]))
    return sorted(set(supported))


def probe_supported_pids(
    transport: str = "sim",
    port_or_addr: str = "",
    baud: int = 38400,
) -> Dict[str, Any]:
    """Query SAE J1979 0100/0120/0140/0160/0180 bitmasks to discover EVERY standard PID supported by this car's ECU."""
    t = str(transport or "sim").strip().lower()
    if t == "sim":
        sim_supported = sorted(pid_catalog.SAE_MODE01_CATALOG.keys())
        return {
            "status": "ok",
            "transport": "sim",
            "supported_count": len(sim_supported),
            "supported_pids": sim_supported,
            "catalog": pid_catalog.list_catalog_metadata(sim_supported),
        }

    supported: List[str] = []
    if t == "tcp":
        elm = TcpSocketELM327(port_or_addr, timeout=2.0)
        elm.open()
        try:
            for c in ("ATE0", "ATL0", "ATS1", "ATAT2"):
                elm.send(c)
            supported = _discover_supported_on_open_elm(elm)
        finally:
            elm.close()
    elif t == "serial":
        elm_s = PosixSerialELM327(port_or_addr, baud=int(baud), timeout=2.0)
        elm_s.open()
        try:
            for c in ("ATE0", "ATL0", "ATS1", "ATAT2"):
                elm_s.send(c)
            supported = _discover_supported_on_open_elm(elm_s)
        finally:
            elm_s.close()
    else:
        init_cmds = ["ATE0", "ATL0", "ATS1", "ATSP0"]
        mask_cmds = ["0100", "0120", "0140", "0160"]
        resps = _run_elm_commands(t, port_or_addr, init_cmds + mask_cmds, baud=baud)
        for mc in mask_cmds:
            base_hex = mc[2:]
            for b in _parse_all_hex_response_bytes(resps.get(mc, ""), "41", base_hex):
                supported.extend(pid_catalog.decode_supported_pid_bitmask(base_hex, b[:4]))
        supported = sorted(set(supported))

    if not supported:
        return {
            "status": "error",
            "transport": t,
            "supported_count": 0,
            "supported_pids": [],
            "message": "ECU returned 0 supported PIDs on 0100–0160. Verify Key-On/Engine-Running and that the Windows BT-OBD Bridge v2.6.4 shows 'ECU: LOCKED'.",
        }
    return {
        "status": "ok",
        "transport": t,
        "supported_count": len(supported),
        "supported_pids": supported,
        "catalog": pid_catalog.list_catalog_metadata(supported),
    }


def elm_sniff_can(
    transport: str = "sim",
    port_or_addr: str = "",
    filter_can_id: str = "",
    duration_sec: float = 2.5,
    dbc_text: str = "",
    baud: int = 38400,
) -> Dict[str, Any]:
    """
    Passive CAN-bus sniff through an ELM327 / STN / Carly SPP adapter using ATH1 + optional ATCRA <id> + ATMA.
    Converts ELM327 ASCII CAN frames into standard candump format and runs full jitter/dropout/byte-heatmap analysis.
    """
    t = str(transport or "sim").strip().lower()
    filt = re.sub(r"[^0-9A-Fa-f]", "", str(filter_can_id or "")).upper()
    if t == "sim":
        raw = generate_sim_can_stream("alternator_ripple_brownout", count=36)
        if filt:
            raw = "\n".join(ln for ln in raw.splitlines() if f" {filt}#" in ln.upper())
        parsed = parse_candump_text(raw, dbc_text=dbc_text)
        parsed["transport"] = "sim"
        parsed["filter_can_id"] = filt
        parsed["raw_sample"] = raw[:4000]
        return parsed

    raw_text = ""
    if t in ("tcp", "serial"):
        elm: Any = TcpSocketELM327(port_or_addr, timeout=2.5) if t == "tcp" else PosixSerialELM327(port_or_addr, baud=int(baud), timeout=2.5)
        elm.open()
        try:
            # Lock onto active CAN protocol BEFORE ATMA! (Avoids ATZ+ATSP0 leaving adapter on K-Line Protocol 5)
            for c in ("ATE0", "ATL1", "ATH1", "ATS0"):
                elm.send(c)
            dpn = elm.send("ATDPN").upper()
            # ELM327 CAN protocols are 6, 7, 8, 9, A, B, C (may be prefixed with 'A' for auto, e.g. 'A6')
            if not re.search(r"[6789ABC]", dpn):
                elm.send("ATSP0")
                elm.send("0100")
                dpn = elm.send("ATDPN").upper()
                if not re.search(r"[6789ABC]", dpn):
                    elm.send("ATSP6")
                    elm.send("0100")
            # Disable ISO-TP CAN auto-formatting so raw broadcast frames pass through cleanly
            elm.send("ATCAF0")
            if filt:
                elm.send("ATAR")
                elm.send(f"ATCRA{filt}")
            else:
                elm.send("ATAR")

            if t == "tcp":
                elm._drain_pending()
                elm._sock.sendall(b"ATMA\r")  # type: ignore
                raw_text = elm.sniff_raw(duration_sec=duration_sec)
            else:
                raw_text = elm.send("ATMA")
        finally:
            # Always restore normal OBD-II framing so Live Stream & OBD scans resume cleanly!
            for rc in ("ATAR", "ATCAF1", "ATH0", "ATL0", "ATS1", "ATAT2", "ATST14"):
                try:
                    elm.send(rc)
                except Exception:
                    pass
            elm.close()
    else:
        return {"status": "error", "message": "ELM327 ATMA CAN sniff supports tcp (Windows BT Bridge / Wi-Fi) or serial transports."}

    # Convert ELM327 header+data lines (e.g. '0C9840A3F2700001000' or '0F11C0100400000') to candump format
    candump_lines: List[str] = []
    t_now = time.time()
    for idx, line in enumerate(re.split(r"[\r\n]+", raw_text)):
        s_ln = line.strip().upper()
        if not s_ln or any(noise in s_ln for noise in _ELM_STATUS_NOISE) or "BUFFER FULL" in s_ln or s_ln == "OK":
            continue
        clean = re.sub(r"[^0-9A-F]", "", s_ln)
        if 5 <= len(clean) <= 19:
            arb = clean[:3]
            payload = clean[3:]
            if len(payload) % 2 == 0 and len(payload) >= 2:
                candump_lines.append(f"({t_now + idx * 0.02:.6f}) elm0 {arb}#{payload}")
    joined = "\n".join(candump_lines)
    parsed = parse_candump_text(joined, dbc_text=dbc_text)
    parsed["transport"] = t
    parsed["filter_can_id"] = filt
    parsed["buffer_full_hit"] = "BUFFER FULL" in raw_text.upper()
    parsed["raw_sample"] = joined[:4000] or raw_text[:1000]
    if parsed["frame_count"] == 0:
        parsed["gateway_hint"] = "Zero passive CAN frames received. Try entering a single 3-char CAN ID filter (e.g. 0C9 or 0F1 on GM) if the bus is busy, or note that ~2018+ vehicles may isolate broadcast CAN behind a Security Gateway (SGW)."
    return parsed


def run_obd_scan(
    transport: str = "sim",
    port_or_addr: str = "",
    sim_profile: str = "lean_misfire",
    baud: int = 38400,
    extra_pids: Optional[List[str]] = None,
    custom_pids: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run a read-only OBD-II diagnostic scan across sim, serial (USB/BT RFCOMM), tcp (Windows BT Bridge / Wi-Fi), or BLE."""
    t = str(transport or "sim").strip().lower()
    if t == "sim":
        return _simulate_obd_scan(sim_profile)

    # Use ATST32 (200ms) during full scans so multi-frame ISO-TP Mode 09 VIN (0902) never truncates after frame 0!
    init_cmds = ["ATE0", "ATL0", "ATS1", "ATAT1", "ATST32", "ATDP", "ATRV"]
    base_pids = ["04", "05", "06", "07", "08", "09", "0A", "0B", "0C", "0D", "0E", "0F", "10", "11", "14", "15", "23", "33", "3C", "42", "44", "46"]
    for ep in (extra_pids or []):
        h = str(ep).upper().replace("01", "", 1).zfill(2)
        if h in pid_catalog.SAE_MODE01_CATALOG and h not in base_pids:
            base_pids.append(h)
    pid_cmds = ["0101"] + [f"01{p}" for p in base_pids]
    dtc_cmds = ["03", "07", "0A", "0902"]

    custom_cmds: List[Tuple[Dict[str, Any], List[str]]] = []
    seq: List[str] = list(init_cmds + pid_cmds + dtc_cmds)
    for cp in (custom_pids or [])[:20]:
        hdr = re.sub(r"[^0-9A-Fa-f]", "", str(cp.get("header") or "7DF")).upper() or "7DF"
        cmd = re.sub(r"[^0-9A-Fa-f]", "", str(cp.get("cmd") or "")).upper()
        if len(cmd) >= 4:
            seq.extend([f"ATSH{hdr}", cmd])
            custom_cmds.append((cp, [f"ATSH{hdr}", cmd]))
    if custom_cmds:
        seq.append("ATSH7DF")

    responses = _run_elm_commands(t, port_or_addr, seq, baud=baud)

    pids: Dict[str, Any] = {}
    for p in base_pids:
        cmd = f"01{p}"
        raw_b = _parse_hex_response_bytes(responses.get(cmd, ""), "41", p)
        decoded = pid_catalog.decode_standard_pid(p, raw_b)
        if decoded:
            k = decoded.pop("key")
            pids[k] = decoded

    # Decode any custom extended PIDs (Torque CSV / OBDb JSON) via strict AST evaluator
    for cp, pair in custom_cmds:
        cmd = pair[1]
        resp_mode = f"{(int(cmd[:2], 16) + 0x40):02X}"
        raw_b = _parse_hex_response_bytes(responses.get(cmd, ""), resp_mode, cmd[2:4])
        if raw_b:
            try:
                val = pid_catalog.evaluate_pid_equation(cp["equation"], raw_b)
                pids[f"custom:{cp['short_name']}"] = {
                    "label": cp["name"],
                    "short": cp["short_name"],
                    "value": val,
                    "unit": cp.get("unit", ""),
                    "min": cp.get("min_val", 0),
                    "max": cp.get("max_val", 100),
                    "custom": True,
                }
            except Exception:
                pass

    dtcs: List[Dict[str, Any]] = []
    for cmd, rmode, label in (("03", "43", "confirmed"), ("07", "47", "pending"), ("0A", "4A", "permanent")):
        for item in _parse_dtc_response(responses.get(cmd, ""), rmode):
            item["state"] = label
            dtcs.append(item)

    bv = _parse_voltage(responses.get("ATRV", ""))
    if bv <= 0.0 and isinstance(pids.get("battery_v"), dict) and pids["battery_v"].get("value"):
        bv = float(pids["battery_v"]["value"])

    vin_str = _parse_vin_response(responses.get("0902", ""))

    readiness_dec = decode_mode01_readiness(_parse_hex_response_bytes(responses.get("0101", ""), "41", "01"))
    if not readiness_dec:
        readiness_dec = {"mil_on": len([d for d in dtcs if d.get("state") == "confirmed"]) > 0}

    return {
        "status": "ok",
        "adapter_type": t,
        "port_or_addr": port_or_addr,
        "vin": vin_str,
        "protocol": responses.get("ATDP", "AUTO").splitlines()[-1] if responses.get("ATDP") else "AUTO",
        "battery_v": bv,
        "pids": pids,
        "dtcs": dtcs,
        "freeze_frame": {},
        "readiness": readiness_dec,
    }


def clear_dtcs_guarded(
    transport: str,
    port_or_addr: str = "",
    confirm_phrase: str = "",
    baud: int = 38400,
) -> Dict[str, Any]:
    """Mode 04 DTC clear — strictly requires explicit operator confirmation phrase 'CLEAR_DTCS'."""
    if str(confirm_phrase).strip() != "CLEAR_DTCS":
        return {
            "status": "blocked",
            "message": "Mode 04 DTC clear refused: explicit confirm_phrase='CLEAR_DTCS' is required. Note: clearing DTCs resets emissions readiness monitors.",
        }
    t = str(transport or "sim").strip().lower()
    if t == "sim":
        return {
            "status": "ok",
            "adapter_type": "sim",
            "cleared": True,
            "message": "Simulated Mode 04 sent: DTCs cleared and readiness monitors reset.",
        }
    if t in ("serial", "tcp", "ble"):
        res = _run_elm_commands(t, port_or_addr, ["ATE0", "04"], baud=baud)
        resp = res.get("04", "")
        return {"status": "ok", "adapter_type": t, "cleared": "44" in resp.upper(), "raw": resp}
    return {"status": "error", "message": f"Unsupported transport: {t}"}


def parse_freeze_frame_csv(text: str) -> Dict[str, Any]:
    """Parse Torque Pro, Car Scanner, or Carly CSV/JSON freeze-frame & PID telemetry exports."""
    s = (text or "").strip()
    if not s:
        return {"status": "error", "message": "Empty freeze-frame input"}
    if s.startswith("{") or s.startswith("["):
        data = json.loads(s)
        return {"status": "ok", "format": "json", "records": data if isinstance(data, list) else [data], "summary": "Parsed JSON OBD-II telemetry"}

    reader = csv.DictReader(io.StringIO(s))
    rows = [dict(r) for r in reader if any((v or "").strip() for v in r.values())]
    if not rows:
        return {"status": "error", "message": "No tabular rows found in CSV"}

    numeric_stats: Dict[str, Dict[str, float]] = {}
    for col in rows[0].keys():
        if not col:
            continue
        vals = []
        for r in rows:
            raw_v = str(r.get(col) or "").strip()
            m = re.match(r"^-?\d+(?:\.\d+)?", raw_v)
            if m:
                vals.append(float(m.group(0)))
        if vals:
            numeric_stats[col.strip()] = {
                "min": round(min(vals), 3),
                "max": round(max(vals), 3),
                "avg": round(sum(vals) / len(vals), 3),
                "last": round(vals[-1], 3),
            }

    anomalies: List[str] = []
    for k, st in numeric_stats.items():
        lk = k.lower()
        if ("ltft" in lk or "long term" in lk or "stft" in lk or "short term" in lk) and (abs(st["max"]) > 12.0 or abs(st["min"]) > 12.0):
            anomalies.append(f"Fuel trim excursion on '{k}': min {st['min']}% / max {st['max']}% (normal ±10%)")
        if "coolant" in lk and st["max"] > 108.0 and st["max"] < 160.0:
            anomalies.append(f"High coolant temp on '{k}': peak {st['max']}°C")
        if ("volt" in lk or "batt" in lk) and 5.0 < st["min"] < 11.8:
            anomalies.append(f"Low battery/ECU voltage dip on '{k}': min {st['min']}V")

    return {
        "status": "ok",
        "format": "csv",
        "row_count": len(rows),
        "columns": [c.strip() for c in rows[0].keys() if c],
        "stats": numeric_stats,
        "anomalies": anomalies,
        "sample_rows": rows[:10],
    }


def parse_dbc(dbc_text: str) -> Dict[int, Dict[str, Any]]:
    """Lightweight pure-Python DBC message (BO_) and signal (SG_) parser."""
    msgs: Dict[int, Dict[str, Any]] = {}
    cur_id: Optional[int] = None
    re_bo = re.compile(r"^\s*BO_\s+(\d+)\s+([A-Za-z0-9_]+)\s*:\s*(\d+)\s+(\S+)")
    re_sg = re.compile(
        r"^\s*SG_\s+([A-Za-z0-9_]+)\s*:\s*(\d+)\|(\d+)@([01])([+-])\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)\s*\[\s*[^]]*\s*\]\s*\"([^\"]*)\""
    )
    for line in (dbc_text or "").splitlines():
        m_bo = re_bo.match(line)
        if m_bo:
            cur_id = int(m_bo.group(1)) & 0x1FFFFFFF
            msgs[cur_id] = {"id": cur_id, "name": m_bo.group(2), "dlc": int(m_bo.group(3)), "signals": []}
            continue
        if cur_id is not None:
            m_sg = re_sg.match(line)
            if m_sg:
                msgs[cur_id]["signals"].append({
                    "name": m_sg.group(1),
                    "start_bit": int(m_sg.group(2)),
                    "length": int(m_sg.group(3)),
                    "little_endian": m_sg.group(4) == "1",
                    "signed": m_sg.group(5) == "-",
                    "scale": float(m_sg.group(6)),
                    "offset": float(m_sg.group(7)),
                    "unit": m_sg.group(8),
                })
    return msgs


def _extract_dbc_signal(payload: bytes, sig: Dict[str, Any]) -> float:
    padded = payload.ljust(8, b"\x00")[:8]
    start = sig["start_bit"]
    length = sig["length"]
    if sig["little_endian"]:
        val_u64 = int.from_bytes(padded, "little")
        raw = (val_u64 >> start) & ((1 << length) - 1)
    else:
        # Motorola / big-endian bit numbering
        bits = "".join(f"{b:08b}" for b in padded)
        byte_idx = start // 8
        bit_in_byte = 7 - (start % 8)
        msb_pos = byte_idx * 8 + bit_in_byte
        raw = int(bits[msb_pos : msb_pos + length] or "0", 2)
    if sig["signed"] and raw >= (1 << (length - 1)):
        raw -= 1 << length
    return round(raw * sig["scale"] + sig["offset"], 4)


def parse_candump_text(text: str, dbc_text: str = "") -> Dict[str, Any]:
    """Parse Linux can-utils `candump` logs + decode ISO-15765 OBD-II responses & optional DBC signals."""
    # Formats supported:
    #   (1700000000.123456) can0 7E8#04410C1AF8000000
    #   can0  7E8   [8]  04 41 0C 1A F8 00 00 00
    re_compact = re.compile(r"^(?:\(([\d.]+)\)\s+)?([A-Za-z0-9_]+)\s+([0-9A-Fa-f]{3,8})#([0-9A-Fa-f]*)")
    re_spaced = re.compile(r"^\s*([A-Za-z0-9_]+)\s+([0-9A-Fa-f]{3,8})\s+\[(\d+)\]\s+([0-9A-Fa-f\s]+)$")

    dbc = parse_dbc(dbc_text) if dbc_text.strip() else {}
    frames: List[Dict[str, Any]] = []
    id_counts: Dict[str, int] = {}
    obd_decoded: List[Dict[str, Any]] = []
    dbc_decoded: List[Dict[str, Any]] = []

    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        ts = None
        iface = "can0"
        arb_hex = ""
        data_hex = ""
        m1 = re_compact.match(s)
        if m1:
            ts = float(m1.group(1)) if m1.group(1) else None
            iface = m1.group(2)
            arb_hex = m1.group(3).upper()
            data_hex = m1.group(4).upper()
        else:
            m2 = re_spaced.match(s)
            if not m2:
                continue
            iface = m2.group(1)
            arb_hex = m2.group(2).upper()
            data_hex = "".join(m2.group(4).split()).upper()

        if len(data_hex) % 2 == 1:
            data_hex = data_hex[:-1]
        data_bytes = [int(data_hex[i : i + 2], 16) for i in range(0, len(data_hex), 2)]
        arb_id = int(arb_hex, 16)
        id_key = f"0x{arb_hex}"
        id_counts[id_key] = id_counts.get(id_key, 0) + 1
        frames.append({"ts": ts, "iface": iface, "id": id_key, "dlc": len(data_bytes), "data": data_hex})

        # ISO 15765-4 standard OBD-II ECU responses (0x7E8 - 0x7EF)
        if 0x7E8 <= arb_id <= 0x7EF and len(data_bytes) >= 3:
            pci = data_bytes[0]
            if pci <= 7 and data_bytes[1] == 0x41 and len(data_bytes) >= 4:
                pid_hex = f"{data_bytes[2]:02X}"
                dec = decode_mode01_pid(pid_hex, data_bytes[3 : 1 + pci])
                if dec:
                    dec["arb_id"] = id_key
                    obd_decoded.append(dec)
            elif pci <= 7 and data_bytes[1] == 0x43:
                payload = data_bytes[2 : 1 + pci]
                idx = 1 if (len(payload) % 2 == 1) else 0
                while idx + 1 < len(payload):
                    code = decode_dtc_bytes(payload[idx], payload[idx + 1])
                    idx += 2
                    if code:
                        obd_decoded.append({"arb_id": id_key, "dtc": lookup_dtc(code)})

        # Optional DBC decoding
        if arb_id in dbc:
            msg = dbc[arb_id]
            raw_b = bytes(data_bytes)
            sig_vals = {sig["name"]: {"value": _extract_dbc_signal(raw_b, sig), "unit": sig["unit"]} for sig in msg["signals"]}
            dbc_decoded.append({"arb_id": id_key, "message": msg["name"], "signals": sig_vals})

    unique_summary = [{"id": k, "count": v} for k, v in sorted(id_counts.items(), key=lambda kv: -kv[1])]
    import signal_analytics  # local module
    stream_stats = signal_analytics.analyze_can_stream(frames)
    return {
        "status": "ok",
        "frame_count": len(frames),
        "unique_ids": unique_summary[:40],
        "obd_decoded": obd_decoded[:60],
        "dbc_decoded": dbc_decoded[:60],
        "sample_frames": frames[:25],
        "bus_analysis": stream_stats,
    }


def generate_sim_can_stream(profile: str = "alternator_ripple_brownout", count: int = 36) -> str:
    """Generate realistic timestamped candump lines (including ECU dropout gaps on brownout profile)."""
    import math
    t0 = time.time() - 2.0
    lines: List[str] = []
    for i in range(count):
        t = t0 + i * 0.04
        # 0x201 ECM Engine Speed / Coolant / Throttle (25 Hz)
        rpm_raw = int((740 + 1100 * max(0.0, math.sin(i * 0.22))) * 4)
        cool_raw = 130  # 90C
        tps_raw = int(38 + 45 * max(0.0, math.sin(i * 0.22)))
        b0, b1 = rpm_raw & 0xFF, (rpm_raw >> 8) & 0xFF
        lines.append(f"({t:.6f}) can0 201#{b0:02X}{b1:02X}{cool_raw:02X}{tps_raw:02X}0000{i & 0xFF:02X}A5")

        # 0x316 ABS/Chassis module — exhibits a dropout gap during voltage brownout
        if not (profile == "alternator_ripple_brownout" and 12 <= i <= 18):
            spd = int(35 + 20 * max(0.0, math.sin(i * 0.18)))
            lines.append(f"({t + 0.008:.6f}) can0 316#{spd:02X}00{spd:02X}00100000{(i * 3) & 0xFF:02X}")

        # 0x420 BCM Voltage & Load telemetry
        v_dec = int((11.2 if (profile == "alternator_ripple_brownout" and 11 <= i <= 19) else 14.1) * 10)
        lines.append(f"({t + 0.015:.6f}) can0 420#{v_dec:02X}010000{(i % 4):02X}0000FF")

        if i == 5:
            lines.append(f"({t + 0.02:.6f}) can0 7E8#04410C1AF8000000")
        if i == 15:
            if profile == "alternator_ripple_brownout":
                lines.append(f"({t + 0.02:.6f}) can0 7E8#064302C100056200")
            else:
                lines.append(f"({t + 0.02:.6f}) can0 7E8#0643020171030200")
    return "\n".join(lines)


def _key_to_pid_hex_map() -> Dict[str, str]:
    m: Dict[str, str] = {}
    for p_hex, meta in pid_catalog.SAE_MODE01_CATALOG.items():
        m[meta["key"]] = p_hex
    # Legacy aliases
    m.update({
        "coolant_temp_c": "05",
        "engine_load_pct": "04",
        "stft_b1_pct": "06",
        "ltft_b1_pct": "07",
        "stft_b2_pct": "08",
        "ltft_b2_pct": "09",
        "timing_adv_deg": "0E",
        "ecu_voltage_v": "42",
    })
    return m


_CORE_TICK_PIDS = ("0C", "05", "04", "06", "07", "11", "10", "0B", "0D", "14", "15", "0E", "0F", "42")


def poll_live_tick_on_elm(
    elm: Any,
    tick_index: int = 0,
    selected_pids: Optional[List[str]] = None,
    supported_pids: Optional[List[str]] = None,
    custom_pids: Optional[List[Dict[str, Any]]] = None,
    use_fast_suffix: bool = False,
) -> Dict[str, Any]:
    """
    Execute one fast, rich multi-channel live telemetry poll directly on an ALREADY-OPEN ELM327 handle.
    Used by both the persistent background collector (buffer-and-drain at ~1-2.5 Hz) and single-shot ticks.
    When use_fast_suffix=True (auto-probed via '010C1'), appends ELM327 '1' response-count hint to Mode 01
    PIDs to cut CAN wait latency from ~200ms -> ~35-55ms per PID without affecting generic clone fallback.
    """
    now_f = time.time()
    k_idx = int(tick_index)
    k2p = _key_to_pid_hex_map()
    sup_set = {str(p).upper().zfill(2) for p in (supported_pids or []) if str(p).strip()}

    poll_pids: List[str] = []

    def _add_pid(h: str, respect_sup: bool = True) -> None:
        hh = h.upper().zfill(2)
        if hh not in pid_catalog.SAE_MODE01_CATALOG or hh in poll_pids:
            return
        if respect_sup and sup_set and hh not in sup_set:
            return
        poll_pids.append(hh)

    for sk in (selected_pids or []):
        if sk == "total_ft_b1":
            _add_pid("06")
            _add_pid("07")
        elif sk in k2p:
            _add_pid(k2p[sk])

    for core_p in _CORE_TICK_PIDS:
        _add_pid(core_p)

    # Rotate 3 additional rich supported catalog PIDs each tick (sweeps full 30-45 channel set cleanly)
    rich_pool = [
        p for p in pid_catalog.SAE_MODE01_CATALOG.keys()
        if p not in poll_pids and (not sup_set or p in sup_set)
    ]
    if rich_pool:
        slice_sz = 3
        start = (k_idx * slice_sz) % len(rich_pool)
        for j in range(min(slice_sz, len(rich_pool))):
            poll_pids.append(rich_pool[(start + j) % len(rich_pool)])

    active_customs = [cp for cp in (custom_pids or []) if cp.get("enabled", 1) and cp.get("cmd")]
    tick_customs: List[Dict[str, Any]] = []
    if active_customs:
        c_start = (k_idx * 2) % len(active_customs)
        for j in range(min(2, len(active_customs))):
            tick_customs.append(active_customs[(c_start + j) % len(active_customs)])

    suffix = "1" if use_fast_suffix else ""
    pid_wire_map: List[Tuple[str, str]] = [(p, f"01{p}{suffix}") for p in poll_pids]

    seq: List[str] = ["ATRV"] + [wire for _, wire in pid_wire_map]
    check_dtcs = (k_idx == 0) or (k_idx % 20 == 0)
    if check_dtcs:
        seq.extend(["03", "07"])

    custom_cmd_pairs: List[Tuple[Dict[str, Any], str]] = []
    for cp in tick_customs:
        hdr = re.sub(r"[^0-9A-Fa-f]", "", str(cp.get("header") or "7DF")).upper() or "7DF"
        ccmd = re.sub(r"[^0-9A-Fa-f]", "", str(cp.get("cmd") or "")).upper()
        if len(ccmd) >= 4:
            seq.extend([f"ATSH{hdr}", ccmd])
            custom_cmd_pairs.append((cp, ccmd))
    if custom_cmd_pairs:
        seq.append("ATSH7DF")

    responses = _send_sequence_failfast(elm, seq, max_consecutive_empty=3)

    decoded_map: Dict[str, float] = {}
    for p, wire in pid_wire_map:
        raw_b = _parse_hex_response_bytes(responses.get(wire, ""), "41", p)
        dec = pid_catalog.decode_standard_pid(p, raw_b)
        if dec and dec.get("value") is not None:
            decoded_map[dec["key"]] = float(dec["value"])

    for cp, ccmd in custom_cmd_pairs:
        try:
            resp_mode = f"{(int(ccmd[:2], 16) + 0x40):02X}"
            raw_b = _parse_hex_response_bytes(responses.get(ccmd, ""), resp_mode, ccmd[2:4])
            if raw_b:
                val = pid_catalog.evaluate_pid_equation(cp["equation"], raw_b)
                decoded_map[f"custom:{cp['short_name']}"] = float(val)
        except Exception:
            pass

    bv = _parse_voltage(responses.get("ATRV", ""))
    if bv <= 0.0 and "battery_v" in decoded_map:
        bv = decoded_map["battery_v"]

    if not decoded_map and bv <= 0.0:
        return {
            "status": "error",
            "message": "No telemetry received from ECU (0 PIDs & 0V). Verify ignition is ON and bridge is active.",
        }

    dtcs: List[Dict[str, Any]] = []
    if check_dtcs:
        for cmd, rmode, label in (("03", "43", "confirmed"), ("07", "47", "pending")):
            for item in _parse_dtc_response(responses.get(cmd, ""), rmode):
                item["state"] = label
                dtcs.append(item)

    fixed_keys = {
        "rpm", "speed_kmh", "coolant_c", "load_pct", "throttle_pct", "maf_gps", "map_kpa",
        "stft_b1", "ltft_b1", "stft_b2", "ltft_b2", "timing_deg", "iat_c", "battery_v",
        "o2_b1s1_v", "o2_b1s2_v", "knock_deg",
    }
    extra_pids = {k: v for k, v in decoded_map.items() if k not in fixed_keys}

    out: Dict[str, Any] = {
        "status": "ok",
        "ts": now_f,
        "rpm": decoded_map.get("rpm"),
        "speed_kmh": decoded_map.get("speed_kmh"),
        "coolant_c": decoded_map.get("coolant_c"),
        "load_pct": decoded_map.get("load_pct"),
        "throttle_pct": decoded_map.get("throttle_pct"),
        "maf_gps": decoded_map.get("maf_gps"),
        "map_kpa": decoded_map.get("map_kpa"),
        "stft_b1": decoded_map.get("stft_b1"),
        "ltft_b1": decoded_map.get("ltft_b1"),
        "stft_b2": decoded_map.get("stft_b2"),
        "ltft_b2": decoded_map.get("ltft_b2"),
        "timing_deg": decoded_map.get("timing_deg"),
        "iat_c": decoded_map.get("iat_c"),
        "battery_v": bv if bv > 0.0 else decoded_map.get("battery_v"),
        "o2_b1s1_v": decoded_map.get("o2_b1s1_v"),
        "o2_b1s2_v": decoded_map.get("o2_b1s2_v"),
        "knock_deg": 0.0,
        "extra_pids": extra_pids,
        "dtcs": dtcs,
        "dtcs_checked": check_dtcs,
        "pids_ok_count": len(decoded_map),
        "fast_frame_mode": use_fast_suffix,
        "vin": "",
        "protocol": "",
    }
    for k_extra, v_extra in extra_pids.items():
        out[k_extra] = v_extra
    return out


def sample_live_tick(
    transport: str = "sim",
    port_or_addr: str = "",
    sim_profile: str = "vacuum_leak_drive",
    tick_index: int = 0,
    baud: int = 38400,
    selected_pids: Optional[List[str]] = None,
    supported_pids: Optional[List[str]] = None,
    custom_pids: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Capture one live telemetry sample (fast pinned/core gauges + rotating rich background ECU PIDs
    for Copilot time-series analytics + periodic DTC check).
    """
    import math
    t = str(transport or "sim").strip().lower()
    now_f = time.time()

    if t != "sim":
        k_idx = int(tick_index)
        sup_list = [str(p).upper().zfill(2) for p in (supported_pids or []) if str(p).strip()]
        discovered_sup: List[str] = []

        if t in ("tcp", "serial"):
            elm: Any = TcpSocketELM327(port_or_addr, timeout=1.6) if t == "tcp" else PosixSerialELM327(port_or_addr, baud=int(baud), timeout=1.6)
            try:
                elm.open()
                proto = ""
                if k_idx == 0:
                    for c in ("ATE0", "ATL0", "ATS1", "ATAT2", "ATST14"):
                        elm.send(c)
                    p_raw = elm.send("ATDP")
                    pl = [ln.strip() for ln in p_raw.splitlines() if ln.strip() and ln.strip().upper() != "ATDP"]
                    proto = pl[-1] if pl else ""
                    if not sup_list:
                        discovered_sup = _discover_supported_on_open_elm(elm)
                        if discovered_sup:
                            sup_list = discovered_sup
                out = poll_live_tick_on_elm(
                    elm=elm,
                    tick_index=k_idx,
                    selected_pids=selected_pids,
                    supported_pids=sup_list or None,
                    custom_pids=custom_pids,
                    use_fast_suffix=False,
                )
                if proto:
                    out["protocol"] = proto
                if discovered_sup:
                    out["discovered_supported_pids"] = discovered_sup
                return out
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
            finally:
                elm.close()

        # BLE single-shot fallback
        k2p = _key_to_pid_hex_map()
        poll_pids = []
        for core_p in _CORE_TICK_PIDS[:10]:
            if core_p in pid_catalog.SAE_MODE01_CATALOG:
                poll_pids.append(core_p)
        seq = (["ATE0", "ATL0", "ATS1"] if k_idx == 0 else []) + ["ATRV"] + [f"01{p}" for p in poll_pids]
        try:
            responses = _run_elm_commands(t, port_or_addr, seq, baud=baud, timeout=2.0)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
        decoded_map: Dict[str, float] = {}
        for p in poll_pids:
            dec = pid_catalog.decode_standard_pid(p, _parse_hex_response_bytes(responses.get(f"01{p}", ""), "41", p))
            if dec and dec.get("value") is not None:
                decoded_map[dec["key"]] = float(dec["value"])
        bv = _parse_voltage(responses.get("ATRV", "")) or decoded_map.get("battery_v", 0.0)
        if not decoded_map and bv <= 0.0:
            return {"status": "error", "message": "No telemetry received over BLE."}
        return {
            "status": "ok", "ts": now_f, "rpm": decoded_map.get("rpm"), "speed_kmh": decoded_map.get("speed_kmh"),
            "coolant_c": decoded_map.get("coolant_c"), "load_pct": decoded_map.get("load_pct"),
            "throttle_pct": decoded_map.get("throttle_pct"), "maf_gps": decoded_map.get("maf_gps"),
            "map_kpa": decoded_map.get("map_kpa"), "stft_b1": decoded_map.get("stft_b1"),
            "ltft_b1": decoded_map.get("ltft_b1"), "stft_b2": decoded_map.get("stft_b2"),
            "ltft_b2": decoded_map.get("ltft_b2"), "timing_deg": decoded_map.get("timing_deg"),
            "iat_c": decoded_map.get("iat_c"), "battery_v": bv if bv > 0.0 else None,
            "o2_b1s1_v": decoded_map.get("o2_b1s1_v"), "o2_b1s2_v": decoded_map.get("o2_b1s2_v"),
            "knock_deg": 0.0, "extra_pids": {}, "dtcs": [], "dtcs_checked": False,
            "pids_ok_count": len(decoded_map), "vin": "", "protocol": "",
        }

    # Dynamic physics drive-cycle generator (idle -> throttle ramp -> cruise -> idle)
    k = int(tick_index)
    phase = (k % 30) / 30.0
    # Smooth drive cycle: idle (0..0.3), accel (0.3..0.55), cruise (0.55..0.8), decel/idle (0.8..1.0)
    if phase < 0.30:
        throttle_wave = 0.0
    elif phase < 0.55:
        throttle_wave = (phase - 0.30) / 0.25
    elif phase < 0.80:
        throttle_wave = 0.72 + 0.08 * math.sin(k * 0.9)
    else:
        throttle_wave = max(0.0, 0.72 * (1.0 - (phase - 0.80) / 0.20))

    jitter = math.sin(k * 2.3) * 0.5 + math.cos(k * 3.7) * 0.3
    rpm = round(710.0 + throttle_wave * 2150.0 + jitter * 22.0, 1)
    speed_kmh = round(max(0.0, throttle_wave * 88.0 + jitter * 1.2), 1)
    load_pct = round(min(96.0, 21.0 + throttle_wave * 54.0 + jitter * 1.5), 1)
    throttle_pct = round(min(85.0, 14.0 + throttle_wave * 42.0), 1)
    maf_gps = round(max(2.8, 4.6 + (rpm - 700.0) * 0.0145 + jitter * 0.15), 2)
    map_kpa = round(32.0 + throttle_wave * 56.0 + jitter * 1.0, 1)
    coolant_c = round(min(94.0, 86.0 + k * 0.22 + math.sin(k * 0.4) * 0.6), 1)
    iat_c = round(29.0 + math.sin(k * 0.2) * 1.5, 1)
    timing_deg = round(12.0 + throttle_wave * 18.0 - jitter * 0.8, 1)
    knock_deg = 0.0

    # Profile-specific fault physics
    prof = str(sim_profile or "vacuum_leak_drive").lower()
    if prof in ("vacuum_leak_drive", "lean_misfire"):
        # Classic vacuum leak: high positive fuel trim at idle (+27% total) dropping toward +6% at cruise RPM!
        ltft_b1 = round(17.5 - throttle_wave * 7.5 + jitter * 0.4, 1)
        stft_b1 = round(10.8 - throttle_wave * 8.2 + math.sin(k * 1.7) * 1.8, 1)
        ltft_b2 = round(3.1 + jitter * 0.3, 1)
        stft_b2 = round(1.8 + math.cos(k * 1.4) * 1.1, 1)
        battery_v = round(14.12 + math.sin(k * 1.3) * 0.05, 2)
        o2_b1s1_v = round(max(0.06, min(0.86, 0.26 + 0.22 * throttle_wave + 0.18 * math.sin(k * 2.1))), 3)
        o2_b1s2_v = round(0.64 + 0.03 * math.sin(k * 0.5), 3)
        if k in (8, 22):
            rpm = round(rpm - 185.0, 1)  # Misfire RPM dip transient
            knock_deg = 2.5
        codes = ["P0171", "P0302"]
    elif prof in ("alternator_ripple_brownout", "can_bus_fault"):
        # Failing alternator diode ripple (0.55Vpp) + load voltage brownout sag to 11.2V -> CAN U0100/U0121
        ltft_b1 = round(2.2 + jitter * 0.4, 1)
        stft_b1 = round(0.9 + math.sin(k * 1.5) * 1.2, 1)
        ltft_b2 = round(1.6 + jitter * 0.3, 1)
        stft_b2 = round(-0.6 + math.cos(k * 1.2) * 1.0, 1)
        sag = -2.2 if (10 <= (k % 30) <= 17) else 0.0
        ripple = 0.28 * math.sin(k * 3.1) + 0.14 * math.cos(k * 5.3)
        battery_v = round(13.45 + sag + ripple, 2)
        o2_b1s1_v = round(0.45 + 0.32 * math.sin(k * 2.2), 3)
        o2_b1s2_v = round(0.67 + 0.03 * math.sin(k * 0.4), 3)
        codes = ["P0562", "U0100", "U0121"]
    elif prof == "catalyst_degraded_o2":
        # Depleted catalyst: downstream O2 B1S2 closely mirrors upstream O2 B1S1 switching
        ltft_b1 = round(1.4 + jitter * 0.3, 1)
        stft_b1 = round(0.6 + math.sin(k * 1.4) * 1.1, 1)
        ltft_b2 = round(1.1 + jitter * 0.3, 1)
        stft_b2 = round(-0.4 + math.cos(k * 1.4) * 1.0, 1)
        battery_v = round(14.18 + math.sin(k * 1.1) * 0.04, 2)
        wave = math.sin(k * 1.9)
        o2_b1s1_v = round(0.48 + 0.34 * wave, 3)
        o2_b1s2_v = round(0.46 + 0.29 * math.sin(k * 1.9 - 0.25), 3)
        codes = ["P0420"]
    else:  # healthy_drive_cycle / healthy
        ltft_b1 = round(1.5 + jitter * 0.3, 1)
        stft_b1 = round(0.5 + math.sin(k * 1.6) * 1.3, 1)
        ltft_b2 = round(0.8 + jitter * 0.3, 1)
        stft_b2 = round(-0.3 + math.cos(k * 1.6) * 1.1, 1)
        battery_v = round(14.20 + math.sin(k * 1.1) * 0.03, 2)
        o2_b1s1_v = round(0.46 + 0.34 * math.sin(k * 2.0), 3)
        o2_b1s2_v = round(0.68 + 0.025 * math.sin(k * 0.4), 3)
        codes = []

    dtcs = [{**lookup_dtc(c), "state": "confirmed"} for c in codes]
    return {
        "status": "ok",
        "ts": now_f,
        "tick_index": k,
        "sim_profile": prof,
        "rpm": rpm,
        "speed_kmh": speed_kmh,
        "coolant_c": coolant_c,
        "load_pct": load_pct,
        "throttle_pct": throttle_pct,
        "maf_gps": maf_gps,
        "map_kpa": map_kpa,
        "stft_b1": stft_b1,
        "ltft_b1": ltft_b1,
        "stft_b2": stft_b2,
        "ltft_b2": ltft_b2,
        "timing_deg": timing_deg,
        "iat_c": iat_c,
        "battery_v": battery_v,
        "o2_b1s1_v": o2_b1s1_v,
        "o2_b1s2_v": o2_b1s2_v,
        "knock_deg": knock_deg,
        "oil_temp_c": round(coolant_c + 6.5, 1),
        "baro_kpa": 100.8,
        "ambient_c": 21,
        "fuel_level_pct": 68.5,
        "evap_purge_pct": round(18.0 + throttle_wave * 32.0, 1),
        "cat_temp_b1s1": round(460.0 + throttle_wave * 240.0, 1),
        "cmd_lambda": round(1.000 - throttle_wave * 0.04, 3),
        "torque_act_pct": round(16.0 + throttle_wave * 58.0, 1),
        "dtcs": dtcs,
        "readiness": {
            "mil_on": len(codes) > 0,
            "misfire": "NOT_READY" if "P0302" in codes else "READY",
            "fuel": "NOT_READY" if "P0171" in codes else "READY",
            "catalyst": "NOT_READY" if "P0420" in codes else "READY",
            "evap": "READY",
            "o2_sensor": "READY",
        },
        "vin": "1GCVKREC0JZ000001",
        "protocol": "ISO 15765-4 (CAN 11-bit 500 kbaud) [SIM]",
    }


def capture_socketcan(interface: str = "vcan0", duration_sec: float = 2.0, max_frames: int = 100, dbc_text: str = "") -> Dict[str, Any]:
    """Capture raw CAN frames natively via Linux socket.AF_CAN (zero external deps)."""
    if not re.match(r"^(v?can|slcan)\d+$", str(interface or "")):
        return {"status": "error", "message": f"Invalid SocketCAN interface name: {interface}"}
    if not hasattr(socket, "AF_CAN"):
        return {"status": "error", "message": "socket.AF_CAN is not available on this OS/kernel."}

    lines: List[str] = []
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        sock.bind((interface,))
        sock.setblocking(False)
        deadline = time.time() + min(15.0, max(0.5, float(duration_sec)))
        while time.time() < deadline and len(lines) < int(max_frames):
            r, _, _ = select.select([sock], [], [], 0.1)
            if not r:
                continue
            pkt = sock.recv(16)
            can_id, dlc = struct.unpack("=IB3x", pkt[:8])
            data = pkt[8 : 8 + dlc]
            is_ext = bool(can_id & 0x80000000)
            clean_id = can_id & (0x1FFFFFFF if is_ext else 0x7FF)
            fmt_id = f"{clean_id:08X}" if is_ext else f"{clean_id:03X}"
            lines.append(f"({time.time():.6f}) {interface} {fmt_id}#{data.hex().upper()}")
    except OSError as e:
        return {"status": "error", "message": f"SocketCAN capture on {interface} failed: {e}"}
    finally:
        sock.close()

    parsed = parse_candump_text("\n".join(lines), dbc_text=dbc_text)
    parsed["interface"] = interface
    parsed["raw_sample"] = "\n".join(lines[:30])
    return parsed
