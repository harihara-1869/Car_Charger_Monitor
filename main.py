#!/usr/bin/env python3
"""
EV Charger Monitor — Enphase API
=================================
Watches the Red phase (L3) on your 3-phase consumption meter.
Alerts you if power drops while you think the car is charging.

Requires: pip install requests plyer python-dotenv

Setup:
  1. Copy .env.example to .env and fill in your credentials
  2. Run: python ev_charger_monitor.py --setup   (to find your system_id)
  3. Run: python ev_charger_monitor.py            (to start monitoring)

Enphase API Plans:
  - Watt (free):     Data is ~15-min delayed. Polling every 5 min is fine.
  - Kilowatt ($249): Live Status API available — much faster detection.
"""

import os
import sys
import time
import json
import base64
import argparse
import smtplib
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ── Optional: desktop notifications ──────────────────────────────────────────
try:
    from plyer import notification as desktop_notify
    DESKTOP_NOTIFY_AVAILABLE = True
except ImportError:
    DESKTOP_NOTIFY_AVAILABLE = False

# ── Optional: .env file support ───────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =============================================================================
# CONFIGURATION  —  edit .env or set environment variables
# =============================================================================

CONFIG = {
    # --- Enphase OAuth credentials (from developer portal) ---
    "CLIENT_ID":          os.getenv("ENPHASE_CLIENT_ID", ""),
    "CLIENT_SECRET":      os.getenv("ENPHASE_CLIENT_SECRET", ""),
    "API_KEY":            os.getenv("ENPHASE_API_KEY", ""),
    "ACCESS_TOKEN":       os.getenv("ENPHASE_ACCESS_TOKEN", ""),
    "REFRESH_TOKEN":      os.getenv("ENPHASE_REFRESH_TOKEN", ""),

    # --- Your Enphase system ---
    "SYSTEM_ID":          os.getenv("ENPHASE_SYSTEM_ID", ""),

    # --- Which phase is your car charger on? ---
    # l1, l2, or l3  (Red phase = l3 in most AU/EU 3-phase wiring)
    "CHARGER_PHASE":      os.getenv("CHARGER_PHASE", "l3"),

    # --- Thresholds ---
    # Watts below this = "charger has stopped"
    "STOP_THRESHOLD_W":   int(os.getenv("STOP_THRESHOLD_W", "500")),
    # Watts above this = "charger is running" (auto-detect charging start)
    "START_THRESHOLD_W":  int(os.getenv("START_THRESHOLD_W", "1000")),

    # --- Polling ---
    # How often to check (seconds). 300 = every 5 min.
    # Free Watt plan: keep at 300+ to stay under 1000 hits/month.
    # Kilowatt plan: you can go as low as 60.
    "POLL_INTERVAL_S":    int(os.getenv("POLL_INTERVAL_S", "300")),

    # --- Notifications ---
    "ALERT_DESKTOP":      os.getenv("ALERT_DESKTOP", "true").lower() == "true",

    # Email (leave EMAIL_TO blank to disable)
    "EMAIL_TO":           os.getenv("EMAIL_TO", ""),
    "EMAIL_FROM":         os.getenv("EMAIL_FROM", ""),
    "SMTP_HOST":          os.getenv("SMTP_HOST", "smtp.gmail.com"),
    "SMTP_PORT":          int(os.getenv("SMTP_PORT", "587")),
    "SMTP_USER":          os.getenv("SMTP_USER", ""),
    "SMTP_PASS":          os.getenv("SMTP_PASS", ""),

    # --- Token storage (auto-saved after refresh) ---
    "TOKEN_FILE":         os.getenv("TOKEN_FILE", ".enphase_tokens.json"),

    # --- Plan type: "watt" or "kilowatt" ---
    "PLAN":               os.getenv("ENPHASE_PLAN", "watt"),
}

BASE_URL = "https://api.enphaseenergy.com/api/v4"
TOKEN_URL = "https://api.enphaseenergy.com/oauth/token"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ev_monitor")

# =============================================================================
# TOKEN MANAGEMENT
# =============================================================================

def load_tokens():
    """Load tokens from file (persists across restarts)."""
    path = Path(CONFIG["TOKEN_FILE"])
    if path.exists():
        with open(path) as f:
            data = json.load(f)
            CONFIG["ACCESS_TOKEN"]  = data.get("access_token",  CONFIG["ACCESS_TOKEN"])
            CONFIG["REFRESH_TOKEN"] = data.get("refresh_token", CONFIG["REFRESH_TOKEN"])
            log.info("Loaded tokens from %s", path)


def save_tokens(access_token, refresh_token):
    """Persist tokens so we survive restarts."""
    CONFIG["ACCESS_TOKEN"]  = access_token
    CONFIG["REFRESH_TOKEN"] = refresh_token
    with open(CONFIG["TOKEN_FILE"], "w") as f:
        json.dump({"access_token": access_token, "refresh_token": refresh_token}, f)
    log.info("Tokens saved to %s", CONFIG["TOKEN_FILE"])


def refresh_access_token():
    """Use the refresh token to get a new access token."""
    if not CONFIG["REFRESH_TOKEN"]:
        log.error("No refresh token — re-run OAuth flow.")
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
    log.info("Access token refreshed ✓")
    return data["access_token"]


def api_headers():
    return {
        "Authorization": f"Bearer {CONFIG['ACCESS_TOKEN']}",
    }


def api_get(path, params=None, retry=True):
    """GET from the Enphase API, auto-refresh token on 401."""
    url = f"{BASE_URL}{path}"
    p = {"key": CONFIG["API_KEY"]}
    if params:
        p.update(params)

    resp = requests.get(url, headers=api_headers(), params=p, timeout=15)

    if resp.status_code == 401 and retry:
        log.warning("Token expired — refreshing...")
        refresh_access_token()
        return api_get(path, params, retry=False)

    if not resp.ok:
        log.error("API error %s: %s", resp.status_code, resp.text[:200])
        return None

    return resp.json()

# =============================================================================
# ENPHASE DATA FETCHING
# =============================================================================

def get_systems():
    """List all systems you have access to."""
    return api_get("/systems")


def get_phase_power_watt_plan(system_id: str) -> dict | None:
    """
    Watt / Kilowatt plan: fetch consumption telemetry.
    Returns {'l1': watts, 'l2': watts, 'l3': watts} for the latest interval.

    Note: Enphase reports energy (Wh) per 15-min interval.
    We convert to average watts: Wh * 4 = W average over that interval.
    """
    data = api_get(f"/systems/{system_id}/telemetry/consumption_meter",
                   params={"granularity": "day"})
    if not data:
        return None

    intervals = data.get("intervals", [])
    if not intervals:
        log.warning("No consumption interval data returned.")
        return None

    # Most recent interval
    latest = intervals[-1]
    phase_data = {}

    # Enphase v4 returns per-phase in 'lines' array: [{enwh: X}, {enwh: Y}, {enwh: Z}]
    lines = latest.get("lines", [])
    phase_keys = ["l1", "l2", "l3"]

    if lines:
        for i, phase in enumerate(phase_keys):
            if i < len(lines):
                enwh = lines[i].get("enwh", 0) or 0
                # enwh over 15 min → average watts = enwh * 4
                phase_data[phase] = round(enwh * 4)
    else:
        # Fallback: site-level only (no per-phase breakdown)
        enwh = latest.get("enwh", 0) or 0
        watts = round(enwh * 4)
        log.warning("Per-phase data not in response — showing site total: %dW", watts)
        phase_data = {"l1": None, "l2": None, "l3": watts, "total": watts}

    interval_end = datetime.fromtimestamp(latest.get("end_at", 0), tz=timezone.utc)
    log.info("Data timestamp: %s (may be up to 15 min old on Watt plan)",
             interval_end.strftime("%H:%M UTC"))

    return phase_data


def get_phase_power_live(system_id: str) -> dict | None:
    """
    Kilowatt / Megawatt plan: Live Status API.
    Returns {'l1': watts, 'l2': watts, 'l3': watts} in near real-time.
    Each live status hit costs $0.10 extra — use sparingly or on Kilowatt plan.
    """
    data = api_get(f"/systems/{system_id}/live_status")
    if not data:
        return None

    phase_data = {}
    # Live status returns consumption_meter with per-phase data
    meters = data.get("consumption_meter", {})
    for phase in ["l1", "l2", "l3"]:
        w = meters.get(f"{phase}_consumption_w") or meters.get(f"{phase}_kw", 0) * 1000
        phase_data[phase] = round(w or 0)

    return phase_data


def get_phase_power(system_id: str) -> dict | None:
    """Route to the right endpoint based on plan."""
    if CONFIG["PLAN"] == "kilowatt":
        return get_phase_power_live(system_id)
    return get_phase_power_watt_plan(system_id)

# =============================================================================
# NOTIFICATIONS
# =============================================================================

def send_desktop_alert(title: str, message: str):
    if not DESKTOP_NOTIFY_AVAILABLE:
        log.warning("Desktop notifications not available — install plyer: pip install plyer")
        return
    try:
        desktop_notify.notify(
            title=title,
            message=message,
            app_name="EV Charger Monitor",
            timeout=30,
        )
    except Exception as e:
        log.warning("Desktop notify failed: %s", e)


def send_email_alert(subject: str, body: str):
    if not CONFIG["EMAIL_TO"]:
        return
    try:
        msg = MIMEMultipart()
        msg["From"]    = CONFIG["EMAIL_FROM"]
        msg["To"]      = CONFIG["EMAIL_TO"]
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(CONFIG["SMTP_HOST"], CONFIG["SMTP_PORT"]) as s:
            s.starttls()
            s.login(CONFIG["SMTP_USER"], CONFIG["SMTP_PASS"])
            s.send_message(msg)
        log.info("📧 Email sent to %s", CONFIG["EMAIL_TO"])
    except Exception as e:
        log.warning("Email failed: %s", e)


def alert(title: str, message: str):
    log.warning("🚨 ALERT: %s — %s", title, message)
    if CONFIG["ALERT_DESKTOP"]:
        send_desktop_alert(title, message)
    send_email_alert(f"EV Monitor: {title}", message)

# =============================================================================
# MONITORING LOOP
# =============================================================================

class ChargerMonitor:
    def __init__(self, system_id: str):
        self.system_id    = system_id
        self.phase        = CONFIG["CHARGER_PHASE"]          # "l3" etc.
        self.stop_thresh  = CONFIG["STOP_THRESHOLD_W"]
        self.start_thresh = CONFIG["START_THRESHOLD_W"]
        self.interval     = CONFIG["POLL_INTERVAL_S"]

        self.charging_active = False   # True = we think it's charging
        self.stop_alert_sent = False   # Avoid repeat alerts
        self.consecutive_low = 0       # Require N low readings before alerting

    def run(self, manual_mode: bool = False):
        """
        manual_mode=True  → alert immediately if power is low (you told it you're charging)
        manual_mode=False → auto-detect: wait for high power, then watch for drop
        """
        log.info("=" * 60)
        log.info("EV Charger Monitor started")
        log.info("  System ID : %s", self.system_id)
        log.info("  Phase     : %s (the Red / L3 line)", self.phase.upper())
        log.info("  Stop alert: power < %dW", self.stop_thresh)
        log.info("  Poll every: %ds", self.interval)
        log.info("  Mode      : %s", "MANUAL (already charging)" if manual_mode else "AUTO-DETECT")
        log.info("=" * 60)

        if manual_mode:
            self.charging_active = True
            log.info("Monitoring active — will alert if %s drops below %dW",
                     self.phase.upper(), self.stop_thresh)

        while True:
            self._tick()
            time.sleep(self.interval)

    def _tick(self):
        now = datetime.now().strftime("%H:%M:%S")
        phase_data = get_phase_power(self.system_id)

        if phase_data is None:
            log.warning("[%s] Could not retrieve data — will retry.", now)
            return

        watts = phase_data.get(self.phase)
        if watts is None:
            log.warning("[%s] No data for phase %s", now, self.phase.upper())
            return

        all_phases = "  |  ".join(
            f"{k.upper()}: {v}W" for k, v in phase_data.items() if v is not None
        )
        log.info("[%s]  %s    ← watching %s", now, all_phases, self.phase.upper())

        # ── Auto-detect charging start ──────────────────────────────────────
        if not self.charging_active:
            if watts >= self.start_thresh:
                self.charging_active = True
                self.stop_alert_sent = False
                self.consecutive_low = 0
                log.info("⚡ Charging DETECTED on %s (%dW) — now monitoring",
                         self.phase.upper(), watts)
            return  # Don't alert until we've seen it start

        # ── Charging is (or was) active — check for stop ────────────────────
        if watts < self.stop_thresh:
            self.consecutive_low += 1
            log.warning("[%s] %s LOW: %dW (reading %d of 2 needed to alert)",
                        now, self.phase.upper(), watts, self.consecutive_low)

            if self.consecutive_low >= 2 and not self.stop_alert_sent:
                msg = (
                    f"⚠️  Car charger appears to have STOPPED!\n"
                    f"Phase {self.phase.upper()} power: {watts}W "
                    f"(threshold: {self.stop_thresh}W)\n"
                    f"Time: {now}\n\n"
                    f"Check your charger — it may have tripped or disconnected."
                )
                alert("Car Charger Stopped!", msg)
                self.stop_alert_sent = True
                self.charging_active = False  # Reset — wait for next charge session

        else:
            # Power is fine — clear the low counter and alert flag
            if self.consecutive_low > 0:
                log.info("Power restored on %s: %dW", self.phase.upper(), watts)
            self.consecutive_low = 0
            self.stop_alert_sent = False

# =============================================================================
# SETUP HELPER
# =============================================================================

def run_setup():
    """Print your system ID and current phase readings to help configure."""
    print("\n=== Enphase Setup Helper ===\n")
    data = get_systems()
    if not data:
        print("❌ Could not reach the API. Check your API_KEY and ACCESS_TOKEN.")
        return

    systems = data.get("systems", [])
    if not systems:
        print("No systems found. Make sure you've approved your app in Enlighten.")
        return

    print(f"Found {len(systems)} system(s):\n")
    for s in systems:
        sid = s["system_id"]
        name = s.get("name") or s.get("public_name", "Unknown")
        status = s.get("status", "?")
        print(f"  ID: {sid}   Name: {name}   Status: {status}")

    print()
    if systems:
        sid = str(systems[0]["system_id"])
        print(f"Fetching phase data for system {sid}...\n")
        phase_data = get_phase_power(sid)
        if phase_data:
            for phase, watts in phase_data.items():
                marker = " ← car charger?" if phase == "l3" else ""
                print(f"  {phase.upper()}: {watts}W{marker}")
        print()
        print(f"Add this to your .env:\n  ENPHASE_SYSTEM_ID={sid}")

# =============================================================================
# .ENV EXAMPLE GENERATOR
# =============================================================================

ENV_EXAMPLE = """\
# ── Enphase API Credentials ────────────────────────────────────────────────
# Get these from https://developer-v4.enphase.com after creating an app
ENPHASE_CLIENT_ID=your_client_id_here
ENPHASE_CLIENT_SECRET=your_client_secret_here
ENPHASE_API_KEY=your_api_key_here

# Run: python ev_charger_monitor.py --setup  to get this
ENPHASE_SYSTEM_ID=your_system_id_here

# OAuth tokens — paste initial tokens here; script auto-refreshes them
ENPHASE_ACCESS_TOKEN=your_access_token_here
ENPHASE_REFRESH_TOKEN=your_refresh_token_here

# ── Monitor Settings ────────────────────────────────────────────────────────
CHARGER_PHASE=l3            # l1, l2, or l3  (Red = l3 in most AU/EU installs)
STOP_THRESHOLD_W=500        # Watts below this → charger stopped
START_THRESHOLD_W=1000      # Watts above this → charger detected (auto mode)
POLL_INTERVAL_S=300         # Seconds between checks (300 = 5 min, safe for free plan)

# Plan: "watt" (free, 15-min data) or "kilowatt" (near real-time, $249/mo)
ENPHASE_PLAN=watt

# ── Notifications ───────────────────────────────────────────────────────────
ALERT_DESKTOP=true          # Desktop pop-up (requires: pip install plyer)

# Email alerts (leave EMAIL_TO blank to disable)
EMAIL_TO=your@email.com
EMAIL_FROM=alerts@yourdomain.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@gmail.com
SMTP_PASS=your_app_password  # Gmail: use App Password, not your real password
"""

# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Monitor your EV charger via Enphase API and alert if it stops."
    )
    parser.add_argument(
        "--setup", action="store_true",
        help="List your systems and current phase readings, then exit."
    )
    parser.add_argument(
        "--manual", action="store_true",
        help="Skip auto-detect — assume charging has already started."
    )
    parser.add_argument(
        "--init-env", action="store_true",
        help="Write a .env.example file to the current directory."
    )
    parser.add_argument(
        "--system-id", type=str, default=None,
        help="Override ENPHASE_SYSTEM_ID from the command line."
    )
    args = parser.parse_args()

    if args.init_env:
        with open(".env.example", "w") as f:
            f.write(ENV_EXAMPLE)
        print("✅ .env.example written. Copy it to .env and fill in your credentials.")
        return

    # Load any saved tokens (overrides stale env vars)
    load_tokens()

    # Validate required config
    missing = [k for k in ("CLIENT_ID", "CLIENT_SECRET", "API_KEY") if not CONFIG[k]]
    if missing:
        print(f"\n❌ Missing config: {', '.join(missing)}")
        print("Run:  python ev_charger_monitor.py --init-env   to create a .env template.\n")
        sys.exit(1)

    if args.setup:
        run_setup()
        return

    system_id = args.system_id or CONFIG["SYSTEM_ID"]
    if not system_id:
        print("\n❌ ENPHASE_SYSTEM_ID not set.")
        print("Run:  python ev_charger_monitor.py --setup   to find your system ID.\n")
        sys.exit(1)

    monitor = ChargerMonitor(system_id=system_id)
    try:
        monitor.run(manual_mode=args.manual)
    except KeyboardInterrupt:
        print("\n\nMonitor stopped.")


if __name__ == "__main__":
    main()