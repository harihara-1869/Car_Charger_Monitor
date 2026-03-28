# Enphase EV Charger Monitor

A Python tool that monitors your EV car charger via the Enphase API and alerts you if it unexpectedly stops charging. It watches a specific phase on your 3-phase consumption meter and sends desktop and/or email notifications when power drops below a threshold.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Main monitoring daemon |
| `enphase_dev.py` | Dev tool — call any endpoint and inspect the raw response |
| `.env` | Your credentials and config (you create this) |
| `.enphase_tokens.json` | Auto-managed token cache (created at first refresh) |

---

## How It Works

The monitor polls the Enphase API every 5 minutes (configurable) and reads the power draw on a specific electrical phase — typically **L3 (Red phase)**, which is where EV chargers are commonly wired in AU/EU installations.

- **Auto mode**: Waits until it detects high power (>1000W by default) on the watched phase, then starts monitoring for a drop
- **Manual mode** (`--manual`): Assumes charging has already started and monitors immediately
- **Alert logic**: Requires 2 consecutive low readings before alerting, to avoid false alarms from brief dips
- Once an alert fires, the monitor resets and waits for the next charging session

**Two API data paths are supported:**

| Plan | Endpoint | Data freshness |
|---|---|---|
| Watt (free) | `/telemetry/consumption_meter` | ~15 min delayed |
| Kilowatt ($249/mo) | `/live_status` | Near real-time, per-phase |

> **Note:** Per-phase data (L1/L2/L3) is only available via the Live Status API. The telemetry endpoint typically returns site-total consumption only. If you can see per-phase data in the Enlighten app under the Live section, set `ENPHASE_PLAN=kilowatt` in your `.env`.

---

## Prerequisites

```bash
pip install requests plyer python-dotenv
```

---

## Step 1 — Get Your Developer Credentials

### 1.1 Create a Developer Account

1. Go to [developer-v4.enphase.com](https://developer-v4.enphase.com)
2. Click **Sign Up** and fill in your details
3. Activate your account via the confirmation email

### 1.2 Create an Application

1. Log in and go to the [Applications](https://developer-v4.enphase.com/admin/applications) page
2. Click **Create Application** and fill in:
   - **Plan**: Start with `Watt` (free). Switch to `Kilowatt` if you need live per-phase data
   - **Name**: Whatever you like — this is shown to the homeowner during authorization
   - **Description**: Brief description of what the app does
   - **Access Controls**: Check at minimum `System Details` and `Site Level Consumption Monitoring`
3. Submit — your app credentials are now generated

### 1.3 Note Your Credentials

From the application page, copy:

- `API Key`
- `Client ID`
- `Client Secret`
- `Auth URL` (shown on the page — looks like `https://api.enphaseenergy.com/oauth/authorize?response_type=code&client_id=YOUR_ID`)

---

## Step 2 — Authorize as the Homeowner (Get OAuth Tokens)

The Enphase API uses OAuth 2.0. You need to go through a one-time browser flow to get your initial access and refresh tokens.

### 2.1 Build the Authorization URL

Take the Auth URL from your app page and append the redirect URI:

```
https://api.enphaseenergy.com/oauth/authorize
  ?response_type=code
  &client_id=YOUR_CLIENT_ID
  &redirect_uri=https://api.enphaseenergy.com/oauth/redirect_uri
```

You can optionally append `&state=some_random_string` for extra security — Enphase echoes it back so you can verify the response.

### 2.2 Authorize in the Browser

1. Open the full URL above in your browser
2. Log in with your **Enlighten** (homeowner) account credentials
3. Click **Approve** on the authorization page
4. You will be redirected to a page at `api.enphaseenergy.com` — look at the URL bar and copy the `code` value from the query string:
   ```
   https://api.enphaseenergy.com/oauth/redirect_uri?code=XXXXXXXX
   ```

> **Important:** The authorization code expires in ~60 seconds and is single-use. Run the next step immediately after copying it.

### 2.3 Exchange the Code for Tokens

On Linux/macOS:
```bash
curl -X POST "https://api.enphaseenergy.com/oauth/token" \
  -H "Authorization: Basic $(printf 'YOUR_CLIENT_ID:YOUR_CLIENT_SECRET' | base64)" \
  -d "grant_type=authorization_code" \
  -d "code=YOUR_CODE_HERE" \
  -d "redirect_uri=https://api.enphaseenergy.com/oauth/redirect_uri"
```

On Windows (Git Bash) — use `printf` instead of `echo -n` to avoid base64 corruption:
```bash
curl -X POST "https://api.enphaseenergy.com/oauth/token" \
  -H "Authorization: Basic $(printf 'YOUR_CLIENT_ID:YOUR_CLIENT_SECRET' | base64)" \
  -d "grant_type=authorization_code" \
  -d "code=YOUR_CODE_HERE" \
  -d "redirect_uri=https://api.enphaseenergy.com/oauth/redirect_uri"
```

You will receive a response like:
```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 86393
}
```

> **Token validity:** Access token lasts **1 day**, refresh token lasts **1 month**. The script auto-refreshes and saves new tokens when the access token expires.

---

## Step 3 — Configure Your .env

### 3.1 Generate the Template

```bash
python main.py --init-env
cp .env.example .env
```

### 3.2 Fill It In

```env
# ── Enphase API Credentials ─────────────────────────────────────
ENPHASE_CLIENT_ID=your_client_id
ENPHASE_CLIENT_SECRET=your_client_secret
ENPHASE_API_KEY=your_api_key

# OAuth tokens — paste the ones you just obtained
ENPHASE_ACCESS_TOKEN=eyJ...
ENPHASE_REFRESH_TOKEN=eyJ...

# Your system ID — find this in Step 4
ENPHASE_SYSTEM_ID=

# ── Monitor Settings ─────────────────────────────────────────────
CHARGER_PHASE=l3            # l1, l2, or l3  (Red = l3 in most AU/EU installs)
STOP_THRESHOLD_W=500        # Watts below this → charger stopped
START_THRESHOLD_W=1000      # Watts above this → charger detected (auto mode)
POLL_INTERVAL_S=300         # Seconds between checks

# watt = free plan (15-min delayed data)
# kilowatt = live per-phase data (required for phase-level monitoring)
ENPHASE_PLAN=watt

# ── Notifications ────────────────────────────────────────────────
ALERT_DESKTOP=true

# Email alerts — leave EMAIL_TO blank to disable
EMAIL_TO=your@email.com
EMAIL_FROM=alerts@yourdomain.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@gmail.com
SMTP_PASS=your_app_password   # Gmail: use an App Password, not your real password
```

### 3.3 Pre-seed the Token Cache

The token file is normally only written when the access token auto-refreshes. Run this once to create it immediately so the script survives restarts from day one:

```bash
python -c "
import os, json
from dotenv import load_dotenv
load_dotenv()
tokens = {
    'access_token':  os.getenv('ENPHASE_ACCESS_TOKEN'),
    'refresh_token': os.getenv('ENPHASE_REFRESH_TOKEN'),
}
with open('.enphase_tokens.json', 'w') as f:
    json.dump(tokens, f, indent=2)
print('Written.')
"
```

---

## Step 4 — Find Your System ID

```bash
python main.py --setup
```

This queries the API and prints all systems you have access to, along with their current per-phase readings:

```
Found 1 system(s):

  ID: 1234567   Name: My Home   Status: normal

Fetching phase data for system 1234567...

  L1: 320W
  L2: 410W
  L3: 7200W  ← car charger?

Add this to your .env:
  ENPHASE_SYSTEM_ID=1234567
```

Add the system ID to your `.env`:

```env
ENPHASE_SYSTEM_ID=1234567
```

---

## Step 5 — Start Monitoring

```bash
# Auto mode — waits to detect charging start, then monitors
python main.py

# Manual mode — assumes charging has already started
python main.py --manual

# Override system ID from the command line
python main.py --system-id 1234567
```

You will see live log output every poll interval:

```
18:05:00  INFO      EV Charger Monitor started
18:05:00  INFO        System ID : 1234567
18:05:00  INFO        Phase     : L3 (the Red / L3 line)
18:05:00  INFO        Stop alert: power < 500W
18:05:00  INFO        Poll every: 300s
18:05:00  INFO        Mode      : AUTO-DETECT
18:05:01  INFO      [18:05:01]  L1: 310W  |  L2: 290W  |  L3: 7340W    ← watching L3
18:05:01  INFO      ⚡ Charging DETECTED on L3 (7340W) — now monitoring
```

Press `Ctrl+C` to stop.

---

## Dev Tool

`enphase_dev.py` lets you call any Enphase API endpoint and see the full raw JSON response. Useful for debugging or exploring what data your system exposes.

```bash
# Interactive menu (recommended starting point)
python enphase_dev.py

# Named endpoints
python enphase_dev.py systems
python enphase_dev.py consumption
python enphase_dev.py live
python enphase_dev.py production
python enphase_dev.py summary
python enphase_dev.py meters

# Any raw path with optional extra params
python enphase_dev.py raw /systems/1234567/summary
python enphase_dev.py raw /systems/1234567/telemetry/production_meter --param granularity=week
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `curl` returns HTML "Something went wrong" | Authorization code expired — get a fresh code from the browser and run curl immediately |
| Base64 looks wrong / auth fails on Windows | Use `printf` instead of `echo -n` in the curl command |
| `Missing config: CLIENT_ID` | `.env` is missing or not in the same directory as the script |
| Monitor shows site total instead of per-phase | Telemetry endpoint doesn't return `lines` data — set `ENPHASE_PLAN=kilowatt` to use the live endpoint |
| Per-phase visible in Enlighten app but not in API | You are on the Watt plan; per-phase is a Kilowatt plan feature via `/live_status` |
| `401 Unauthorized` on every call | Access token is invalid — re-run the OAuth browser flow (Step 2) to get fresh tokens |
| Refresh token expired | Refresh tokens last 1 month — if expired, repeat Step 2 entirely |

---

## Notes on API Rate Limits & Cost

- The **Watt (free) plan** allows ~1000 API calls/month. At the default 5-minute interval the monitor makes ~288 calls/day — only run it while actively intending to charge, or raise `POLL_INTERVAL_S`
- The **Kilowatt plan** Live Status endpoint may incur per-call charges if you are not subscribed to the paid tier — verify your plan before setting `ENPHASE_PLAN=kilowatt`
- Tokens are shared between `main.py` and `enphase_dev.py` via `.enphase_tokens.json` — a refresh triggered by one script automatically updates the token used by the other