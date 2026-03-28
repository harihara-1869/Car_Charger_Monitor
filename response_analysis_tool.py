#!/usr/bin/env python3
"""
Enphase API Dev Tool
====================
Calls any Enphase v4 endpoint and pretty-prints the full response.
Reuses the same .env and token file as the EV charger monitor.

Usage:
  python enphase_dev.py                          # interactive menu
  python enphase_dev.py systems                  # call a named endpoint
  python enphase_dev.py raw /systems             # call any raw path
  python enphase_dev.py raw /systems/{id}/summary --param start_date=2024-01-01

Requires: pip install requests python-dotenv
"""

import os
import sys
import json
import base64
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone

# ── Optional .env support ─────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =============================================================================
# CONFIG  — mirrors main.py exactly so the same .env works
# =============================================================================

CONFIG = {
    "CLIENT_ID":      os.getenv("ENPHASE_CLIENT_ID", ""),
    "CLIENT_SECRET":  os.getenv("ENPHASE_CLIENT_SECRET", ""),
    "API_KEY":        os.getenv("ENPHASE_API_KEY", ""),
    "ACCESS_TOKEN":   os.getenv("ENPHASE_ACCESS_TOKEN", ""),
    "REFRESH_TOKEN":  os.getenv("ENPHASE_REFRESH_TOKEN", ""),
    "SYSTEM_ID":      os.getenv("ENPHASE_SYSTEM_ID", ""),
    "TOKEN_FILE":     os.getenv("TOKEN_FILE", ".enphase_tokens.json"),
}

BASE_URL  = "https://api.enphaseenergy.com/api/v4"
TOKEN_URL = "https://api.enphaseenergy.com/oauth/token"

# =============================================================================
# KNOWN ENDPOINTS  (add more as you discover them)
# =============================================================================

ENDPOINTS = {
    # ── System list ──────────────────────────────────────────────────────────
    "systems": {
        "path": "/systems",
        "desc": "List all systems you have access to",
        "params": {},
    },

    # ── System summary ───────────────────────────────────────────────────────
    "summary": {
        "path": "/systems/{system_id}/summary",
        "desc": "High-level summary: status, production today, lifetime",
        "params": {},
    },

    # ── Telemetry ─────────────────────────────────────────────────────────────
    "production": {
        "path": "/systems/{system_id}/telemetry/production_meter",
        "desc": "Site-level production meter telemetry (Wh per interval)",
        "params": {"granularity": "day"},
    },
    "consumption": {
        "path": "/systems/{system_id}/telemetry/consumption_meter",
        "desc": "Site-level consumption meter telemetry — includes per-phase data",
        "params": {"granularity": "day"},
    },
    "battery": {
        "path": "/systems/{system_id}/telemetry/battery",
        "desc": "Battery charge/discharge telemetry",
        "params": {"granularity": "day"},
    },

    # ── Live status (Kilowatt plan) ───────────────────────────────────────────
    "live": {
        "path": "/systems/{system_id}/live_status",
        "desc": "Near real-time power data (Kilowatt plan only)",
        "params": {},
    },

    # ── Devices ──────────────────────────────────────────────────────────────
    "inverters": {
        "path": "/systems/{system_id}/devices/microinverters",
        "desc": "List all microinverters in the system",
        "params": {},
    },
    "meters": {
        "path": "/systems/{system_id}/devices/meters",
        "desc": "List meters attached to the system",
        "params": {},
    },
    "encharges": {
        "path": "/systems/{system_id}/devices/encharges",
        "desc": "List Encharge (battery) devices",
        "params": {},
    },

    # ── Energy ───────────────────────────────────────────────────────────────
    "energy_today": {
        "path": "/systems/{system_id}/energy_today",
        "desc": "Production and consumption energy totals for today",
        "params": {},
    },
    "energy_lifetime": {
        "path": "/systems/{system_id}/energy_lifetime",
        "desc": "Lifetime energy production (daily breakdown)",
        "params": {},
    },
}

# =============================================================================
# TOKEN MANAGEMENT  (mirrors main.py)
# =============================================================================

def load_tokens():
    path = Path(CONFIG["TOKEN_FILE"])
    if path.exists():
        with open(path) as f:
            data = json.load(f)
            CONFIG["ACCESS_TOKEN"]  = data.get("access_token",  CONFIG["ACCESS_TOKEN"])
            CONFIG["REFRESH_TOKEN"] = data.get("refresh_token", CONFIG["REFRESH_TOKEN"])
        print(f"[auth] Tokens loaded from {path}")


def save_tokens(access_token, refresh_token):
    CONFIG["ACCESS_TOKEN"]  = access_token
    CONFIG["REFRESH_TOKEN"] = refresh_token
    with open(CONFIG["TOKEN_FILE"], "w") as f:
        json.dump({"access_token": access_token, "refresh_token": refresh_token}, f)
    print(f"[auth] New tokens saved to {CONFIG['TOKEN_FILE']}")


def refresh_access_token():
    if not CONFIG["REFRESH_TOKEN"]:
        print("[auth] ERROR: No refresh token — re-run OAuth flow.")
        sys.exit(1)
    creds = base64.b64encode(
        f"{CONFIG['CLIENT_ID']}:{CONFIG['CLIENT_SECRET']}".encode()
    ).decode()
    resp = requests.post(
        TOKEN_URL,
        params={"grant_type": "refresh_token", "refresh_token": CONFIG["REFRESH_TOKEN"]},
        headers={"Authorization": f"Basic {creds}"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    save_tokens(data["access_token"], data["refresh_token"])
    print("[auth] Access token refreshed ✓")
    return data["access_token"]

# =============================================================================
# API CALL
# =============================================================================

def api_get(path: str, params: dict = None, retry: bool = True):
    url = f"{BASE_URL}{path}"
    p = {"key": CONFIG["API_KEY"]}
    if params:
        p.update(params)

    print(f"\n[request]  GET {url}")
    print(f"[params]   {p}")
    print(f"[headers]  Authorization: Bearer {CONFIG['ACCESS_TOKEN'][:20]}...")
    print()

    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {CONFIG['ACCESS_TOKEN']}"},
        params=p,
        timeout=15,
    )

    print(f"[response] HTTP {resp.status_code}  ({len(resp.content)} bytes)")
    print(f"[headers]  Content-Type: {resp.headers.get('Content-Type', '?')}")
    print()

    if resp.status_code == 401 and retry:
        print("[auth] Token expired — refreshing...")
        refresh_access_token()
        return api_get(path, params, retry=False)

    # Pretty-print full response regardless of status
    try:
        data = resp.json()
        print(json.dumps(data, indent=2))
        return data
    except Exception:
        print(resp.text)
        return None

# =============================================================================
# INTERACTIVE MENU
# =============================================================================

def interactive_menu():
    system_id = CONFIG["SYSTEM_ID"]

    print("\n╔══════════════════════════════════════╗")
    print("║     Enphase API Dev Tool             ║")
    print("╚══════════════════════════════════════╝\n")

    if system_id:
        print(f"  System ID from .env: {system_id}\n")
    else:
        print("  ⚠  No ENPHASE_SYSTEM_ID in .env — system-specific endpoints won't work.\n")

    print("  Available endpoints:\n")
    names = list(ENDPOINTS.keys())
    for i, name in enumerate(names):
        ep = ENDPOINTS[name]
        path = ep["path"].replace("{system_id}", system_id or "<system_id>")
        print(f"  [{i+1:2d}]  {name:<20}  {ep['desc']}")
        print(f"        {path}")
        print()

    print(f"  [{len(names)+1:2d}]  raw                  Call any custom path")
    print()

    choice = input("  Pick a number (or 'q' to quit): ").strip()
    if choice.lower() == 'q':
        return

    try:
        idx = int(choice) - 1
    except ValueError:
        print("Invalid choice.")
        return

    if idx == len(names):  # raw
        path = input("  Path (e.g. /systems/123456/summary): ").strip()
        extra = input("  Extra params as key=value,key2=value2 (or blank): ").strip()
        params = parse_params(extra)
        api_get(path, params)
    elif 0 <= idx < len(names):
        name = names[idx]
        ep = ENDPOINTS[name]
        path = ep["path"].replace("{system_id}", system_id)
        params = dict(ep["params"])

        # Ask for extra params
        extra = input(f"  Extra params (or blank to use defaults {params}): ").strip()
        if extra:
            params.update(parse_params(extra))

        api_get(path, params)
    else:
        print("Out of range.")


def parse_params(s: str) -> dict:
    """Parse 'key=value,key2=value2' into a dict."""
    result = {}
    if not s:
        return result
    for pair in s.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            result[k.strip()] = v.strip()
    return result

# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Enphase API dev tool — pretty-prints full responses."
    )
    parser.add_argument(
        "endpoint", nargs="?", default=None,
        help=f"Named endpoint ({', '.join(ENDPOINTS)}) or 'raw'"
    )
    parser.add_argument(
        "path", nargs="?", default=None,
        help="Raw path when using 'raw' mode, e.g. /systems/123/summary"
    )
    parser.add_argument(
        "--param", action="append", default=[],
        metavar="KEY=VALUE",
        help="Extra query param (repeatable): --param granularity=week"
    )
    parser.add_argument(
        "--system-id", default=None,
        help="Override ENPHASE_SYSTEM_ID"
    )
    args = parser.parse_args()

    load_tokens()

    # Validate credentials
    missing = [k for k in ("CLIENT_ID", "CLIENT_SECRET", "API_KEY") if not CONFIG[k]]
    if missing:
        print(f"\n❌ Missing config: {', '.join(missing)}")
        print("Make sure your .env is filled in.\n")
        sys.exit(1)

    if args.system_id:
        CONFIG["SYSTEM_ID"] = args.system_id

    extra_params = {}
    for p in args.param:
        if "=" in p:
            k, v = p.split("=", 1)
            extra_params[k] = v

    if args.endpoint is None:
        interactive_menu()

    elif args.endpoint == "raw":
        if not args.path:
            print("Usage: enphase_dev.py raw /your/path [--param key=value]")
            sys.exit(1)
        api_get(args.path, extra_params or None)

    elif args.endpoint in ENDPOINTS:
        ep = ENDPOINTS[args.endpoint]
        system_id = CONFIG["SYSTEM_ID"]
        path = ep["path"].replace("{system_id}", system_id)
        params = dict(ep["params"])
        params.update(extra_params)
        api_get(path, params)

    else:
        print(f"Unknown endpoint '{args.endpoint}'.")
        print(f"Known: {', '.join(ENDPOINTS)} or 'raw'")
        sys.exit(1)


if __name__ == "__main__":
    main()