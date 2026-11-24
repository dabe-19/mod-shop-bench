#!/usr/bin/env python3
"""
tools/vin_decoder.py — Zero-Key Vehicle Architecture & VIN Decoder (NHTSA vPIC + Offline WMI Fallback).

Decodes any 17-character ISO 3779 VIN into Year, Make, Model, Trim, Engine Displacement/Cylinders,
Fuel Delivery (GDI vs Port vs Diesel), Forced Induction (Turbo/Supercharged vs NA), Drive Type,
and practical architecture notes so the Mechanic Copilot never makes generic misassumptions
(e.g. recommending mechanical fuel rail gauges on high-pressure GDI rails or nonexistent inline filters).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# ISO 3779 10th-character Model Year map (modern 2001-2030 cycle; disambiguates 1980-2000 via char 7)
_YEAR_CODES_MODERN = {
    "1": 2001, "2": 2002, "3": 2003, "4": 2004, "5": 2005, "6": 2006, "7": 2007, "8": 2008, "9": 2009,
    "A": 2010, "B": 2011, "C": 2012, "D": 2013, "E": 2014, "F": 2015, "G": 2016, "H": 2017, "J": 2018,
    "K": 2019, "L": 2020, "M": 2021, "N": 2022, "P": 2023, "R": 2024, "S": 2025, "T": 2026, "V": 2027,
    "W": 2028, "X": 2029, "Y": 2030,
}

_WMI_MAKES = {
    "1G1": "Chevrolet", "1G2": "Pontiac", "1G3": "Oldsmobile", "1G4": "Buick", "1G6": "Cadillac",
    "1GC": "Chevrolet", "1GT": "GMC", "2G1": "Chevrolet", "2G4": "Buick", "3G1": "Chevrolet",
    "1FA": "Ford", "1FB": "Ford", "1FC": "Ford", "1FD": "Ford", "1FM": "Ford", "1FT": "Ford", "2FM": "Ford", "3FA": "Ford",
    "1C3": "Chrysler", "1C4": "Jeep", "1C6": "Ram", "1D7": "Dodge", "2C3": "Chrysler", "2C4": "Chrysler",
    "1HG": "Honda", "2HG": "Honda", "5FN": "Honda", "JHM": "Honda", "19U": "Acura",
    "1NX": "Toyota", "2T1": "Toyota", "4T1": "Toyota", "5TD": "Toyota", "JT2": "Toyota", "JTD": "Toyota", "JTE": "Toyota", "JTH": "Lexus",
    "1N4": "Nissan", "1N6": "Nissan", "3N1": "Nissan", "JN1": "Nissan", "JN8": "Nissan",
    "4S3": "Subaru", "4S4": "Subaru", "JF1": "Subaru", "JF2": "Subaru",
    "JM1": "Mazda", "JM3": "Mazda", "3MZ": "Mazda",
    "KM8": "Hyundai", "KMH": "Hyundai", "5NP": "Hyundai", "KNA": "Kia", "KND": "Kia", "5XY": "Kia",
    "WBA": "BMW", "WBS": "BMW", "5UX": "BMW", "WDB": "Mercedes-Benz", "WDD": "Mercedes-Benz", "4JG": "Mercedes-Benz",
    "WAU": "Audi", "WVW": "Volkswagen", "3VW": "Volkswagen", "1VW": "Volkswagen", "WP0": "Porsche", "YV1": "Volvo",
    "5YJ": "Tesla", "7SA": "Tesla", "7PD": "Rivian",
}


def normalize_vin(vin: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(vin or "")).upper()


def _offline_vin_fallback(v: str) -> Dict[str, Any]:
    yr = str(_YEAR_CODES_MODERN.get(v[9], "")) if len(v) == 17 else ""
    make = _WMI_MAKES.get(v[:3], "")
    return {
        "status": "ok",
        "source": "offline_wmi",
        "vin": v,
        "year": yr,
        "make": make,
        "model": "",
        "trim": "",
        "body_class": "",
        "engine_cylinders": "",
        "displacement_l": "",
        "engine_model": "",
        "fuel_type": "",
        "fuel_delivery": "",
        "turbo": "",
        "drive_type": "",
        "transmission": "",
        "is_turbo": False,
        "is_gdi": False,
        "is_diesel": False,
        "is_hybrid_or_ev": False,
        "engine_summary": "",
        "architecture_notes": [
            "Decoded offline via ISO 3779 WMI/Year only (NHTSA vPIC unreachable); confirm exact engine aspiration and fuel injection type before intrusive testing."
        ],
    }


def _build_architecture_notes(info: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    if info.get("is_gdi"):
        notes.append(
            "Direct Injection (GDI/SGDI): Uses a two-stage fuel system (in-tank low-pressure lift pump ~350–450 kPa + cam-driven HPFP 2,000–20,000+ kPa). "
            "Monitor OBD PIDs 010A (low-side) and 0123 (GDI rail) rather than attaching a mechanical rail gauge to the high-pressure side; many modern GDI vehicles use a non-serviceable in-tank sock filter."
        )
    if info.get("is_turbo"):
        notes.append(
            "Forced Induction (Turbocharged): Intake MAP exceeds barometric pressure (~100 kPa) under boost. "
            "Check post-MAF charge pipes, intercooler boots, bypass/wastegate plumbing, and turbo-specific PCV check valves whenever fuel trims drift."
        )
    if info.get("is_diesel"):
        notes.append(
            "Compression-Ignition (Diesel): Fuel-trim / stoichiometric O2 rules do not apply; inspect rail pressure, boost/VGT, EGR, and DPF differential pressure."
        )
    if info.get("is_hybrid_or_ev"):
        notes.append(
            "Electrified / Hybrid Powertrain: Engine-off zero-RPM states and DC-DC converter voltage regulation (13.2–14.8V) are normal; verify HV safety before probing."
        )
    if not notes:
        notes.append("Verify OEM fuel-injection type (port vs GDI) and filter serviceability before recommending physical fuel-rail intrusion.")
    return notes


def decode_vin(vin: str, allow_network: bool = True, timeout: float = 5.0) -> Dict[str, Any]:
    """Decode a 17-character VIN using NHTSA vPIC (free, zero API keys) with automatic offline fallback."""
    v = normalize_vin(vin)
    if not VIN_RE.match(v):
        return {"status": "error", "vin": v, "message": f"Invalid 17-character VIN: '{vin}'"}

    base = _offline_vin_fallback(v)
    if not allow_network:
        return base

    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{v}?format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "BlueLodge-ShopBench/2.6.6", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        rows = raw.get("Results") or []
        if not rows or not isinstance(rows[0], dict):
            return base
        r = rows[0]

        def _g(*keys: str) -> str:
            for k in keys:
                val = str(r.get(k) or "").strip()
                if val and val.lower() not in ("not applicable", "null", "none"):
                    return val
            return ""

        yr = _g("ModelYear") or base["year"]
        make = _g("Make") or base["make"]
        model = _g("Model")
        trim = _g("Trim", "Series", "Series2")
        body = _g("BodyClass")
        cyl = _g("EngineCylinders")
        disp = _g("DisplacementL")
        if disp:
            try:
                disp = f"{float(disp):.1f}L"
            except ValueError:
                pass
        eng_model = _g("EngineModel")
        fuel_type = _g("FuelTypePrimary")
        fuel_deliv = _g("FuelInjectionType")
        turbo = _g("Turbo")
        drive = _g("DriveType")
        trans = _g("TransmissionStyle")
        other_eng = _g("OtherEngineInfo")

        blob = " ".join([eng_model, fuel_deliv, turbo, other_eng, fuel_type]).lower()
        is_turbo = ("yes" in turbo.lower()) or ("turbo" in blob) or ("supercharg" in blob)
        is_gdi = any(tok in blob for tok in ("direct injection", "gdi", "sgdi", "sidi", "tfsi", "fsi", "ecoboost", "ltg"))
        is_diesel = "diesel" in fuel_type.lower()
        is_hev = any(tok in blob for tok in ("hybrid", "electric", "phev", "bev"))

        parts = [p for p in [disp, f"L{cyl}" if cyl and cyl.isdigit() else cyl, "Turbo" if is_turbo else "", "GDI" if is_gdi else "", eng_model] if p]
        eng_summary = " ".join(parts).strip()

        out: Dict[str, Any] = {
            "status": "ok",
            "source": "nhtsa_vpic",
            "vin": v,
            "year": yr,
            "make": make,
            "model": model,
            "trim": trim,
            "body_class": body,
            "engine_cylinders": cyl,
            "displacement_l": disp,
            "engine_model": eng_model,
            "fuel_type": fuel_type,
            "fuel_delivery": fuel_deliv,
            "turbo": turbo,
            "drive_type": drive,
            "transmission": trans,
            "other_engine_info": other_eng,
            "is_turbo": is_turbo,
            "is_gdi": is_gdi,
            "is_diesel": is_diesel,
            "is_hybrid_or_ev": is_hev,
            "engine_summary": eng_summary,
            "display_name": " ".join(x for x in [yr, make, model, trim, eng_summary] if x).strip(),
        }
        out["architecture_notes"] = _build_architecture_notes(out)
        return out
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return base
