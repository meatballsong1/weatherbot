"""
WeatherWatch Discord Bot
========================
Pulls NWS/IEM alerts and products, posts to Discord with full customization.
/settings  — interactive panel to configure everything
/wx        — current conditions for your station
/forecast  — NWS forecast discussion
/alerts    — current active alerts

Dependencies:
    pip install discord.py aiohttp

Setup:
    1. Create a bot at discord.dev, enable message content intent
    2. Set BOT_TOKEN below (or via env var WEATHERWATCH_TOKEN)
    3. Invite bot with scopes: bot + applications.commands
    4. Run: python weather_bot.py
"""

import asyncio
from dotenv import load_dotenv
load_dotenv()
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.getenv("WEATHERWATCH_TOKEN", "")
_brevo_key  = os.getenv("BREVO_SMTP_KEY", "")
CONFIG_FILE = Path("weatherwatch_config.json")
LOG_FILE    = Path("weatherwatch.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger("weatherwatch")

# ── Default config ─────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # Station / location
    "station":          "OAX",          # NWS office ID  (OAX = Omaha)
    "zone":             "NEZ040",       # NWS zone for alerts
    "state":            "NE",           # 2-letter state
    "lat":              41.26,          # for point-based forecast
    "lon":              -96.01,

    # Discord targeting
    "alert_channel_id":     0,          # channel to post alerts
    "product_channel_id":   0,          # channel for text products (AFD, HWO, etc.)
    "ping_role_id":         0,          # role to ping for significant alerts
    "everyone_events":      ["Tornado Emergency", "Tornado Warning", "Tornado Watch"],

    # Alert filters — which NWS event types to post
    "enabled_alerts": {
        "Tornado Emergency":          True,
        "Tornado Warning":            True,
        "Tornado Watch":              True,
        "Severe Thunderstorm Warning":True,
        "Severe Thunderstorm Watch":  True,
        "Flash Flood Emergency":      True,
        "Flash Flood Warning":        True,
        "Flash Flood Watch":          True,
        "Red Flag Warning":           True,
        "Fire Weather Watch":         True,
        "Winter Storm Warning":       True,
        "Winter Storm Watch":         True,
        "Blizzard Warning":           True,
        "Ice Storm Warning":          True,
        "Hazardous Weather Outlook":  False,
        "Area Forecast Discussion":   False,
        "Special Weather Statement":  True,
        "Dust Storm Warning":         True,
        "Extreme Cold Warning":       True,
        "Extreme Heat Warning":       True,
        "Dense Fog Advisory":         False,
        "Wind Advisory":              False,
        "High Wind Warning":          True,
        "High Wind Watch":            True,
    },

    # IEM text products to monitor (all disabled by default)
    "iem_products": {
        "AFD":  False,   # Area Forecast Discussion
        "HWO":  False,   # Hazardous Weather Outlook
        "SPS":  True,    # Special Weather Statement
        "RVD":  False,   # River Forecast Discussion
        "PNS":  False,   # Public Information Statement
        "LSR":  True,    # Local Storm Report
        "SVR":  True,    # Severe Thunderstorm Warning text
        "TOR":  True,    # Tornado Warning text
        "FFW":  True,    # Flash Flood Warning text
        "FWW":  True,    # Red Flag Warning text
        "FFA":  False,   # Flash Flood Watch text
        "WOU":  False,   # Tornado/Severe Thunderstorm Watch outline update
    },

    # Tornado emergency config — multiple @everyone pings
    "tornado_everyone_count": 10,    # how many times to ping @everyone
    "tornado_everyone_delay": 2,     # seconds between each ping

    # Behavior
    "poll_interval_secs":   60,      # how often to check for new alerts
    "post_all_clear":       True,    # post when an alert expires
    "embed_style":          "rich",  # "rich" | "compact" | "minimal"
    "show_affected_areas":  True,
    "show_expiry":          True,
    "show_source":          True,

    # SMS alerts via email-to-SMS gateway
    "sms_enabled":        False,
    "sms_to":             "4027180244@tmomail.net",  # number@carrier_gateway
    "sms_gmail_address":  "",    # your gmail address
    "sms_gmail_password": "",    # gmail app password (not your login password)
    "sms_events": [
        "Tornado Emergency",
        "Tornado Warning",
        "Tornado Watch",
        "Severe Thunderstorm Warning",
        "Severe Thunderstorm Watch",
    ],

    # SMS alerts via email-to-carrier gateway
    "sms_enabled":    False,
    "sms_number":     "4027180244",
    "sms_carrier":    "verizon",       # verizon | att | tmobile | sprint
    "sms_from":       "weather@oofbomb.xyz",
    "sms_from_name":  "weather bot",
    "sms_method":     "api",           # "api" (default) or "smtp"
    "sms_api_key":    "",              # Brevo API key — set via .env BREVO_API_KEY
    "sms_smtp_host":  "smtp-relay.brevo.com",
    "sms_smtp_port":  587,
    "sms_smtp_user":  "a664fc001@smtp-brevo.com",
    "sms_smtp_pass":  "",              # set via .env BREVO_SMTP_KEY
    "sms_events": [
        "Tornado Emergency",
        "Tornado Warning",
        "Tornado Watch",
        "Severe Thunderstorm Warning",
        "Severe Thunderstorm Watch",
    ],

    # Seen tracking
    "_seen_alerts":   [],
    "_seen_products": [],
}
# Inject Brevo keys from env
_brevo_api_key = os.getenv("BREVO_API_KEY", "")
if _brevo_key:
    DEFAULT_CONFIG["sms_smtp_pass"] = _brevo_key
if _brevo_api_key:
    DEFAULT_CONFIG["sms_api_key"] = _brevo_api_key

# ── Severity colors ───────────────────────────────────────────────────────────
SEVERITY_COLORS = {
    "Tornado Emergency":           0xff0000,
    "Tornado Warning":             0xff2200,
    "Tornado Watch":               0xff6600,
    "Severe Thunderstorm Warning": 0xff8800,
    "Severe Thunderstorm Watch":   0xffaa00,
    "Flash Flood Emergency":       0x9900ff,
    "Flash Flood Warning":         0x44aaff,
    "Flash Flood Watch":           0x2266cc,
    "Red Flag Warning":            0xff4400,
    "Fire Weather Watch":          0xff7700,
    "Winter Storm Warning":        0x88aaff,
    "Winter Storm Watch":          0x6688cc,
    "Blizzard Warning":            0xaabbff,
    "Ice Storm Warning":           0x99ccff,
    "High Wind Warning":           0xffcc00,
    "High Wind Watch":             0xffdd44,
    "Special Weather Statement":   0x00ccaa,
    "Hazardous Weather Outlook":   0x00aacc,
    "Area Forecast Discussion":    0x5588aa,
    "Dust Storm Warning":          0xcc8844,
    "Extreme Cold Warning":        0x0044ff,
    "Extreme Heat Warning":        0xff4400,
}

SEVERITY_EMOJI = {
    "Tornado Emergency":           "🚨",
    "Tornado Warning":             "🌪️",
    "Tornado Watch":               "⚠️",
    "Severe Thunderstorm Warning": "⛈️",
    "Severe Thunderstorm Watch":   "🌩️",
    "Flash Flood Emergency":       "🚨",
    "Flash Flood Warning":         "🌊",
    "Flash Flood Watch":           "💧",
    "Red Flag Warning":            "🔥",
    "Fire Weather Watch":          "🔥",
    "Winter Storm Warning":        "🌨️",
    "Winter Storm Watch":          "❄️",
    "Blizzard Warning":            "🌬️",
    "Ice Storm Warning":           "🧊",
    "High Wind Warning":           "💨",
    "High Wind Watch":             "💨",
    "Special Weather Statement":   "📋",
    "Hazardous Weather Outlook":   "📋",
    "Area Forecast Discussion":    "📄",
    "Dust Storm Warning":          "🌫️",
    "Extreme Cold Warning":        "🥶",
    "Extreme Heat Warning":        "🥵",
}

# ── SMS via email-to-carrier gateway ─────────────────────────────────────────
CARRIER_GATEWAYS = {
    "verizon":  "@vtext.com",
    "att":      "@txt.att.net",
    "tmobile":  "@tmomail.net",
    "sprint":   "@messaging.sprintpcs.com",
    "boost":    "@sms.myboostmobile.com",
    "cricket":  "@sms.cricketwireless.net",
    "metro":    "@mymetropcs.com",
    "uscellular":"@email.uscc.net",
}

async def send_sms_alert(event: str, headline: str, areas: str):
    """Send SMS via email-to-carrier gateway using Brevo API (default) or SMTP."""
    if not cfg.get("sms_enabled"):
        return
    if event not in cfg.get("sms_events", []):
        return

    number    = cfg.get("sms_number", "").strip().replace("-","").replace(" ","").replace("+1","")
    carrier   = cfg.get("sms_carrier", "verizon").lower()
    from_addr = cfg.get("sms_from", "weather@oofbomb.xyz")
    from_name = cfg.get("sms_from_name", "weather bot")
    method    = cfg.get("sms_method", "api")

    if not number:
        log.warning("SMS: no phone number configured")
        return

    gateway = CARRIER_GATEWAYS.get(carrier, "@vtext.com")
    to_addr = f"{number}{gateway}"
    subject = event[:50]
    body    = f"{event}\n{headline[:80]}\n{areas[:60]}"

    if method == "api":
        # ── Brevo API (default) ──────────────────────────────────────────────
        api_key = cfg.get("sms_api_key", "") or os.getenv("BREVO_API_KEY", "")
        if not api_key:
            log.warning("SMS: no Brevo API key — set BREVO_API_KEY in .env or /settings SMS")
            return
        payload = {
            "sender":      {"name": from_name, "email": from_addr},
            "to":          [{"email": to_addr}],
            "subject":     subject,
            "textContent": body,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.brevo.com/v3/smtp/email",
                    json=payload,
                    headers={
                        "accept":       "application/json",
                        "content-type": "application/json",
                        "api-key":      api_key,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status in (200, 201):
                        log.info(f"SMS sent via Brevo API to {to_addr}: {event}")
                    else:
                        txt = await r.text()
                        log.warning(f"SMS Brevo API error {r.status}: {txt}")
        except Exception as e:
            log.warning(f"SMS API failed: {e}")

    else:
        # ── SMTP fallback ────────────────────────────────────────────────────
        smtp_host = cfg.get("sms_smtp_host", "smtp-relay.brevo.com")
        smtp_port = int(cfg.get("sms_smtp_port", 587))
        smtp_user = cfg.get("sms_smtp_user", "a664fc001@smtp-brevo.com")
        smtp_pass = cfg.get("sms_smtp_pass", "") or os.getenv("BREVO_SMTP_KEY", "")
        if not smtp_pass:
            log.warning("SMS: no SMTP key — set BREVO_SMTP_KEY in .env or /settings SMS")
            return
        try:
            import smtplib
            from email.mime.text import MIMEText
            msg            = MIMEText(body)
            msg["From"]    = f"{from_name} <{from_addr}>"
            msg["To"]      = to_addr
            msg["Subject"] = subject
            loop = asyncio.get_event_loop()
            def _send():
                with smtplib.SMTP(smtp_host, smtp_port) as s:
                    s.starttls()
                    s.login(smtp_user, smtp_pass)
                    s.send_message(msg)
            await loop.run_in_executor(None, _send)
            log.info(f"SMS sent via SMTP to {to_addr}: {event}")
        except Exception as e:
            log.warning(f"SMS SMTP failed: {e}")

# ── Config helpers ────────────────────────────────────────────────────────────
def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(saved)
            # Merge nested dicts
            for k in ("enabled_alerts", "iem_products"):
                if k in saved:
                    cfg[k] = {**DEFAULT_CONFIG[k], **saved[k]}
            return cfg
        except Exception as e:
            log.warning(f"Config load error: {e}")
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    # Don't save massive seen lists if they get huge
    out = dict(cfg)
    out["_seen_alerts"]   = cfg["_seen_alerts"][-500:]
    out["_seen_products"] = cfg["_seen_products"][-500:]
    CONFIG_FILE.write_text(json.dumps(out, indent=2))

cfg = load_config()


# ── SMS via email-to-SMS ──────────────────────────────────────────────────────
# Common carrier gateways:
#   T-Mobile:  number@tmomail.net
#   Verizon:   number@vtext.com
#   AT&T:      number@txt.att.net
#   Sprint:    number@messaging.sprintpcs.com
#   US Cellular: number@email.uscc.net
# Gmail: use an App Password (myaccount.google.com/apppasswords)

import smtplib
from email.mime.text import MIMEText

async def send_sms_alert(event: str, headline: str, areas: str):
    """Send SMS via email-to-SMS gateway using Gmail SMTP."""
    if not cfg.get("sms_enabled"):
        return
    if event not in cfg.get("sms_events", []):
        return

    gmail = cfg.get("sms_gmail_address", "").strip()
    passw = cfg.get("sms_gmail_password", "").strip()
    to    = cfg.get("sms_to", "").strip()

    if not gmail or not passw or not to:
        log.warning("SMS: gmail_address, gmail_password, or sms_to not configured")
        return

    emoji = {"Tornado Emergency":"🚨","Tornado Warning":"🌪️","Tornado Watch":"⚠️",
             "Severe Thunderstorm Warning":"⛈️","Severe Thunderstorm Watch":"🌩️"}.get(event, "⚠️")

    body = f"{emoji} {event}\n{headline[:100]}\n{areas[:80]}"

    try:
        msg = MIMEText(body)
        msg["From"]    = gmail
        msg["To"]      = to
        msg["Subject"] = ""  # SMS gateways ignore subject

        def _send():
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as s:
                s.login(gmail, passw)
                s.sendmail(gmail, to, msg.as_string())

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send)
        log.info(f"SMS sent: {event} → {to}")
    except Exception as e:
        log.warning(f"SMS failed: {e}")

# ── NWS API helpers ───────────────────────────────────────────────────────────
NWS_BASE = "https://api.weather.gov"
IEM_BASE = "https://mesonet.agron.iastate.edu"

async def nws_get(session: aiohttp.ClientSession, path: str) -> dict | None:
    url = NWS_BASE + path
    try:
        async with session.get(url, headers={"User-Agent": "WeatherWatchBot/1.0"}, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        log.warning(f"NWS GET {path}: {e}")
    return None

async def fetch_active_alerts(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch active NWS alerts for the configured zone/state."""
    zone = cfg.get("zone", "")
    state = cfg.get("state", "NE")
    data = None
    if zone:
        data = await nws_get(session, f"/alerts/active?zone={zone}")
    if not data:
        data = await nws_get(session, f"/alerts/active?area={state}")
    if not data:
        return []
    return data.get("features", [])

async def fetch_iem_products(session: aiohttp.ClientSession, ptype: str, station: str) -> list[dict]:
    """Fetch recent IEM text products."""
    url = f"{IEM_BASE}/json/nwstext_search.py?station={station}&product={ptype}&limit=5"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                return data.get("results", [])
    except Exception as e:
        log.warning(f"IEM {ptype}: {e}")
    return []

async def fetch_point_forecast(session: aiohttp.ClientSession, lat: float, lon: float) -> str:
    """Fetch short forecast for a lat/lon."""
    meta = await nws_get(session, f"/points/{lat},{lon}")
    if not meta:
        return "Forecast unavailable."
    props = meta.get("properties", {})
    forecast_url = props.get("forecast", "")
    if not forecast_url:
        return "Forecast unavailable."
    path = forecast_url.replace(NWS_BASE, "")
    data = await nws_get(session, path)
    if not data:
        return "Forecast unavailable."
    periods = data.get("properties", {}).get("periods", [])[:3]
    lines = []
    for p in periods:
        lines.append(f"**{p['name']}**: {p['detailedForecast']}")
    return "\n\n".join(lines) or "No forecast data."

# ── Embed builders ────────────────────────────────────────────────────────────
def build_alert_embed(alert: dict) -> discord.Embed:
    props = alert.get("properties", {})
    event    = props.get("event", "Unknown Alert")
    headline = props.get("headline", "")
    desc     = props.get("description", "")[:1800]
    instr    = props.get("instruction", "")[:600]
    severity = props.get("severity", "")
    urgency  = props.get("urgency", "")
    onset    = props.get("onset", "")
    expires  = props.get("expires", "")
    areas    = props.get("areaDesc", "")
    sender   = props.get("senderName", "NWS")
    msg_type = props.get("messageType", "Alert")

    emoji = SEVERITY_EMOJI.get(event, "⚠️")
    color = SEVERITY_COLORS.get(event, 0xffaa00)

    style = cfg.get("embed_style", "rich")

    if style == "minimal":
        emb = discord.Embed(
            title=f"{emoji} {event}",
            description=headline or desc[:300],
            color=color,
        )
        return emb

    emb = discord.Embed(
        title=f"{emoji} {event}",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    if headline:
        emb.description = f"**{headline}**"

    if desc and style == "rich":
        # Truncate intelligently
        chunks = desc.split("\n\n")
        body = "\n\n".join(chunks[:4])[:1500]
        emb.add_field(name="📋 Details", value=body or "—", inline=False)

    if instr and style == "rich":
        emb.add_field(name="🔔 Instructions", value=instr[:500] or "—", inline=False)

    if cfg.get("show_affected_areas") and areas:
        # Truncate long area lists
        area_short = areas[:400] + ("…" if len(areas) > 400 else "")
        emb.add_field(name="📍 Affected Areas", value=area_short, inline=False)

    meta_parts = []
    if severity:   meta_parts.append(f"**Severity:** {severity}")
    if urgency:    meta_parts.append(f"**Urgency:** {urgency}")
    if msg_type:   meta_parts.append(f"**Type:** {msg_type}")
    if meta_parts:
        emb.add_field(name="ℹ️ Info", value=" · ".join(meta_parts), inline=False)

    if cfg.get("show_expiry") and expires:
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            emb.add_field(name="⏰ Expires", value=f"<t:{int(exp_dt.timestamp())}:F>", inline=True)
        except Exception:
            emb.add_field(name="⏰ Expires", value=expires, inline=True)

    if onset:
        try:
            on_dt = datetime.fromisoformat(onset.replace("Z", "+00:00"))
            emb.add_field(name="🕐 Onset", value=f"<t:{int(on_dt.timestamp())}:F>", inline=True)
        except Exception:
            pass

    if cfg.get("show_source") and sender:
        emb.set_footer(text=f"Source: {sender} · NWS Alerts API")

    return emb

def build_product_embed(product: dict, ptype: str) -> discord.Embed:
    text = product.get("text", "")[:3000]
    issued = product.get("valid", "") or product.get("utc_valid", "")
    station = product.get("station", cfg.get("station",""))

    labels = {
        "AFD": ("📄 Area Forecast Discussion", 0x5588aa),
        "HWO": ("⚠️ Hazardous Weather Outlook", 0x00aacc),
        "SPS": ("📋 Special Weather Statement", 0x00ccaa),
        "LSR": ("⚡ Local Storm Report", 0xff8800),
        "TOR": ("🌪️ Tornado Warning", 0xff2200),
        "SVR": ("⛈️ Severe Thunderstorm Warning", 0xff8800),
        "FFW": ("🌊 Flash Flood Warning", 0x44aaff),
        "FWW": ("🔥 Red Flag Warning", 0xff4400),
        "PNS": ("📢 Public Information Statement", 0x8899aa),
        "WOU": ("📊 Watch Outline Update", 0xffaa00),
    }
    title, color = labels.get(ptype, (f"📋 NWS Product: {ptype}", 0x8899aa))

    emb = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))

    # Clean and truncate the product text
    clean = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(clean) > 2800:
        clean = clean[:2800] + "\n\n*[truncated — see NWS for full text]*"

    emb.description = f"```\n{clean}\n```"

    if issued:
        try:
            dt = datetime.fromisoformat(issued.replace("Z", "+00:00"))
            emb.set_footer(text=f"{station} · Issued {dt.strftime('%b %d, %I:%M %p %Z')}")
        except Exception:
            emb.set_footer(text=f"{station}")

    return emb

def build_all_clear_embed(event: str, areas: str) -> discord.Embed:
    color = SEVERITY_COLORS.get(event, 0x00cc66)
    emb = discord.Embed(
        title=f"✅ {event} — Expired / All Clear",
        description=f"The **{event}** for portions of the area has expired.",
        color=0x00cc66,
        timestamp=datetime.now(timezone.utc),
    )
    if areas:
        emb.add_field(name="📍 Previously Affected", value=areas[:300], inline=False)
    return emb

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

@bot.event
async def on_ready():
    log.info(f"WeatherWatch online as {bot.user}")
    await tree.sync()
    poll_loop.start()
    log.info("Poll loop started")

# ── Polling loop ──────────────────────────────────────────────────────────────
@tasks.loop(seconds=60)
async def poll_loop():
    poll_loop.change_interval(seconds=max(30, cfg.get("poll_interval_secs", 60)))
    async with aiohttp.ClientSession() as session:
        await check_alerts(session)
        await check_products(session)

async def check_alerts(session: aiohttp.ClientSession):
    alert_ch_id = cfg.get("alert_channel_id", 0)
    if not alert_ch_id:
        return

    channel = bot.get_channel(alert_ch_id)
    if not channel:
        return

    alerts = await fetch_active_alerts(session)
    seen   = set(cfg["_seen_alerts"])
    role_id = cfg.get("ping_role_id", 0)

    for alert in alerts:
        props   = alert.get("properties", {})
        aid     = props.get("id", "")
        event   = props.get("event", "")
        areas   = props.get("areaDesc", "")

        if not aid or not event:
            continue

        enabled = cfg["enabled_alerts"].get(event, False)
        if not enabled:
            continue

        if aid in seen:
            continue

        # Mark seen
        cfg["_seen_alerts"].append(aid)
        save_config(cfg)

        log.info(f"New alert: {event} ({aid})")

        # Send SMS for configured events
        asyncio.create_task(send_sms_alert(
            event,
            props.get("headline", event),
            props.get("areaDesc", ""),
        ))

        emb = build_alert_embed(alert)

        # Build ping string
        is_everyone_event = event in cfg.get("everyone_events", [])
        ping_str = ""
        if is_everyone_event:
            ping_str = "@everyone "
        elif role_id:
            ping_str = f"<@&{role_id}> "

        # Send the embed
        try:
            await channel.send(content=ping_str.strip() or None, embed=emb)
        except Exception as e:
            log.error(f"Failed to send alert embed: {e}")
            continue

        # SMS notification
        asyncio.create_task(send_sms_alert(
            event,
            props.get("headline", ""),
            areas,
        ))

        # Tornado emergency — spam @everyone N times
        if event in ("Tornado Emergency", "Tornado Warning") and is_everyone_event:
            count = cfg.get("tornado_everyone_count", 10)
            delay = cfg.get("tornado_everyone_delay", 2)
            for i in range(count - 1):
                await asyncio.sleep(delay)
                try:
                    await channel.send(
                        f"🚨 **TORNADO EMERGENCY** 🚨 @everyone — {areas[:100] or 'see above'} "
                        f"({i+2}/{count})"
                    )
                except Exception:
                    pass

    # All-clear: check if anything we've seen is no longer active
    if cfg.get("post_all_clear"):
        active_ids = {a.get("properties", {}).get("id") for a in alerts}
        # We'd need to track what was active last time — simplified: just log
        pass

async def check_products(session: aiohttp.ClientSession):
    prod_ch_id = cfg.get("product_channel_id", 0)
    if not prod_ch_id:
        return
    channel = bot.get_channel(prod_ch_id)
    if not channel:
        return

    station = cfg.get("station", "OAX")
    seen    = set(cfg["_seen_products"])

    for ptype, enabled in cfg["iem_products"].items():
        if not enabled:
            continue
        products = await fetch_iem_products(session, ptype, station)
        for prod in products:
            pid = prod.get("id") or prod.get("pil") or prod.get("valid", "") + ptype
            if not pid or pid in seen:
                continue
            cfg["_seen_products"].append(pid)
            save_config(cfg)
            emb = build_product_embed(prod, ptype)
            try:
                await channel.send(embed=emb)
                log.info(f"Posted IEM product: {ptype} {pid}")
            except Exception as e:
                log.error(f"Failed to post product {ptype}: {e}")

# ── /settings command ─────────────────────────────────────────────────────────
class SettingsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    def settings_embed(self) -> discord.Embed:
        emb = discord.Embed(
            title="⛈️ WeatherWatch Settings",
            description="Configure your weather alert bot below. Use the buttons to edit each section.",
            color=0x0099ff,
            timestamp=datetime.now(timezone.utc),
        )

        # Station
        emb.add_field(
            name="📡 Station & Location",
            value=(
                f"**Station:** `{cfg.get('station','OAX')}`\n"
                f"**Zone:** `{cfg.get('zone','NEZ040')}`\n"
                f"**State:** `{cfg.get('state','NE')}`\n"
                f"**Lat/Lon:** `{cfg.get('lat',0)}, {cfg.get('lon',0)}`"
            ),
            inline=True,
        )

        # Channels
        ach = cfg.get("alert_channel_id", 0)
        pch = cfg.get("product_channel_id", 0)
        rid = cfg.get("ping_role_id", 0)
        emb.add_field(
            name="📢 Discord Targets",
            value=(
                f"**Alert Channel:** {f'<#{ach}>' if ach else '`Not set`'}\n"
                f"**Products Channel:** {f'<#{pch}>' if pch else '`Not set`'}\n"
                f"**Ping Role:** {f'<@&{rid}>' if rid else '`Not set`'}\n"
                f"**Poll Interval:** `{cfg.get('poll_interval_secs',60)}s`"
            ),
            inline=True,
        )

        # Alert toggles summary
        enabled = [k for k, v in cfg["enabled_alerts"].items() if v]
        disabled = [k for k, v in cfg["enabled_alerts"].items() if not v]
        emb.add_field(
            name=f"🚨 Alerts ({len(enabled)} enabled)",
            value="\n".join(f"✅ {e}" for e in enabled[:10]) + ("\n…" if len(enabled) > 10 else "") or "None",
            inline=False,
        )

        # Product toggles
        prod_on = [k for k, v in cfg["iem_products"].items() if v]
        emb.add_field(
            name=f"📄 IEM Products ({len(prod_on)} enabled)",
            value=" · ".join(prod_on) if prod_on else "All disabled",
            inline=False,
        )

        # Tornado config
        emb.add_field(
            name="🌪️ Tornado Emergency",
            value=(
                f"**@everyone pings:** `{cfg.get('tornado_everyone_count',10)}x`\n"
                f"**Delay between pings:** `{cfg.get('tornado_everyone_delay',2)}s`\n"
                f"**@everyone events:** {', '.join(cfg.get('everyone_events',[]))}"
            ),
            inline=False,
        )

        # Behavior
        emb.add_field(
            name="⚙️ Behavior",
            value=(
                f"**Embed style:** `{cfg.get('embed_style','rich')}`\n"
                f"**Show affected areas:** `{cfg.get('show_affected_areas',True)}`\n"
                f"**Show expiry:** `{cfg.get('show_expiry',True)}`\n"
                f"**Post all-clear:** `{cfg.get('post_all_clear',True)}`"
            ),
            inline=True,
        )

        # SMS
        sms_on = cfg.get("sms_enabled", False)
        carrier = cfg.get("sms_carrier","verizon")
        gateway = CARRIER_GATEWAYS.get(carrier,"@vtext.com")
        emb.add_field(
            name="📱 SMS Alerts",
            value=(
                f"**Status:** {'✅ Enabled' if sms_on else '⛔ Disabled'}\n"
                f"**Number:** `{cfg.get('sms_number','not set')}{gateway}`\n"
                f"**Events:** {', '.join(cfg.get('sms_events',[]))}"
            ),
            inline=False,
        )

        emb.set_footer(text="WeatherWatch · Settings panel — changes save immediately")
        return emb

    @discord.ui.button(label="📡 Station", style=discord.ButtonStyle.secondary, row=0)
    async def btn_station(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StationModal())

    @discord.ui.button(label="📢 Channels", style=discord.ButtonStyle.secondary, row=0)
    async def btn_channels(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ChannelsModal())

    @discord.ui.button(label="🚨 Alert Toggles", style=discord.ButtonStyle.primary, row=0)
    async def btn_alerts(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=discord.Embed(title="🚨 Alert Toggles", color=0xff6600,
                description="Use the select menu to toggle individual alert types."),
            view=AlertToggleView(),
            ephemeral=True,
        )

    @discord.ui.button(label="📄 IEM Products", style=discord.ButtonStyle.primary, row=0)
    async def btn_products(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            embed=discord.Embed(title="📄 IEM Text Products", color=0x5588aa,
                description="Toggle which NWS text products get posted to your products channel."),
            view=ProductToggleView(),
            ephemeral=True,
        )

    @discord.ui.button(label="🌪️ Tornado Config", style=discord.ButtonStyle.danger, row=1)
    async def btn_tornado(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TornadoModal())

    @discord.ui.button(label="⚙️ Behavior", style=discord.ButtonStyle.secondary, row=1)
    async def btn_behavior(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BehaviorModal())

    @discord.ui.button(label="📱 SMS Alerts", style=discord.ButtonStyle.secondary, row=2)
    async def btn_sms(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SmsModal())

    @discord.ui.button(label="📱 SMS Alerts", style=discord.ButtonStyle.secondary, row=2)
    async def btn_sms(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SmsModal())

    @discord.ui.button(label="🔄 Reset Seen", style=discord.ButtonStyle.danger, row=1)
    async def btn_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg["_seen_alerts"]   = []
        cfg["_seen_products"] = []
        save_config(cfg)
        await interaction.response.send_message("✅ Seen alert/product history cleared — will repost on next poll.", ephemeral=True)

    @discord.ui.button(label="🧪 Test Alert", style=discord.ButtonStyle.secondary, row=1)
    async def btn_test(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Use `/test` to send a test alert with full ping config applied.", ephemeral=True)

# ── Modals ────────────────────────────────────────────────────────────────────
class StationModal(discord.ui.Modal, title="Station & Location Settings"):
    station = discord.ui.TextInput(label="NWS Station ID", placeholder="OAX", max_length=4)
    zone    = discord.ui.TextInput(label="NWS Zone (e.g. NEZ040)", placeholder="NEZ040", max_length=10)
    state   = discord.ui.TextInput(label="State (2-letter)", placeholder="NE", max_length=2)
    lat     = discord.ui.TextInput(label="Latitude", placeholder="41.26")
    lon     = discord.ui.TextInput(label="Longitude", placeholder="-96.01")

    def __init__(self):
        super().__init__()
        self.station.default = cfg.get("station", "OAX")
        self.zone.default    = cfg.get("zone", "NEZ040")
        self.state.default   = cfg.get("state", "NE")
        self.lat.default     = str(cfg.get("lat", 41.26))
        self.lon.default     = str(cfg.get("lon", -96.01))

    async def on_submit(self, interaction: discord.Interaction):
        cfg["station"] = self.station.value.upper().strip()
        cfg["zone"]    = self.zone.value.upper().strip()
        cfg["state"]   = self.state.value.upper().strip()
        try:    cfg["lat"] = float(self.lat.value)
        except: pass
        try:    cfg["lon"] = float(self.lon.value)
        except: pass
        save_config(cfg)
        await interaction.response.send_message(
            f"✅ Station set to **{cfg['station']}** · Zone **{cfg['zone']}** · State **{cfg['state']}**",
            ephemeral=True)

class ChannelsModal(discord.ui.Modal, title="Discord Channel & Role Settings"):
    alert_ch   = discord.ui.TextInput(label="Alert Channel ID", placeholder="123456789012345678")
    product_ch = discord.ui.TextInput(label="Products Channel ID", placeholder="123456789012345678")
    role_id    = discord.ui.TextInput(label="Ping Role ID (for significant alerts)", placeholder="123456789012345678", required=False)
    poll_iv    = discord.ui.TextInput(label="Poll Interval (seconds, min 30)", placeholder="60")

    def __init__(self):
        super().__init__()
        self.alert_ch.default   = str(cfg.get("alert_channel_id", "")) if cfg.get("alert_channel_id") else ""
        self.product_ch.default = str(cfg.get("product_channel_id", "")) if cfg.get("product_channel_id") else ""
        self.role_id.default    = str(cfg.get("ping_role_id", "")) if cfg.get("ping_role_id") else ""
        self.poll_iv.default    = str(cfg.get("poll_interval_secs", 60))

    async def on_submit(self, interaction: discord.Interaction):
        try:    cfg["alert_channel_id"]   = int(self.alert_ch.value.strip())
        except: pass
        try:    cfg["product_channel_id"] = int(self.product_ch.value.strip())
        except: pass
        try:    cfg["ping_role_id"]       = int(self.role_id.value.strip()) if self.role_id.value.strip() else 0
        except: pass
        try:    cfg["poll_interval_secs"] = max(30, int(self.poll_iv.value.strip()))
        except: pass
        save_config(cfg)
        await interaction.response.send_message("✅ Channel settings saved.", ephemeral=True)

class TornadoModal(discord.ui.Modal, title="Tornado Emergency Settings"):
    count    = discord.ui.TextInput(label="@everyone ping count (per Tornado Emergency)", placeholder="10")
    delay    = discord.ui.TextInput(label="Seconds between @everyone pings", placeholder="2")
    everyone = discord.ui.TextInput(
        label="Events that trigger @everyone (comma-separated)",
        placeholder="Tornado Emergency, Tornado Warning",
        style=discord.TextStyle.paragraph,
    )

    def __init__(self):
        super().__init__()
        self.count.default    = str(cfg.get("tornado_everyone_count", 10))
        self.delay.default    = str(cfg.get("tornado_everyone_delay", 2))
        self.everyone.default = ", ".join(cfg.get("everyone_events", []))

    async def on_submit(self, interaction: discord.Interaction):
        try:    cfg["tornado_everyone_count"] = max(1, min(20, int(self.count.value)))
        except: pass
        try:    cfg["tornado_everyone_delay"] = max(1, int(self.delay.value))
        except: pass
        cfg["everyone_events"] = [e.strip() for e in self.everyone.value.split(",") if e.strip()]
        save_config(cfg)
        await interaction.response.send_message(
            f"✅ Tornado config saved — **{cfg['tornado_everyone_count']}x** @everyone pings "
            f"with **{cfg['tornado_everyone_delay']}s** delay.",
            ephemeral=True,
        )

class BehaviorModal(discord.ui.Modal, title="Behavior Settings"):
    style       = discord.ui.TextInput(label="Embed style (rich / compact / minimal)", placeholder="rich")
    show_areas  = discord.ui.TextInput(label="Show affected areas? (true/false)", placeholder="true")
    show_expiry = discord.ui.TextInput(label="Show expiry time? (true/false)", placeholder="true")
    all_clear   = discord.ui.TextInput(label="Post all-clear on expiry? (true/false)", placeholder="true")

    def __init__(self):
        super().__init__()
        self.style.default       = cfg.get("embed_style", "rich")
        self.show_areas.default  = str(cfg.get("show_affected_areas", True)).lower()
        self.show_expiry.default = str(cfg.get("show_expiry", True)).lower()
        self.all_clear.default   = str(cfg.get("post_all_clear", True)).lower()

    async def on_submit(self, interaction: discord.Interaction):
        s = self.style.value.strip().lower()
        if s in ("rich","compact","minimal"):
            cfg["embed_style"] = s
        cfg["show_affected_areas"] = self.show_areas.value.strip().lower() == "true"
        cfg["show_expiry"]         = self.show_expiry.value.strip().lower() == "true"
        cfg["post_all_clear"]      = self.all_clear.value.strip().lower() == "true"
        save_config(cfg)
        await interaction.response.send_message("✅ Behavior settings saved.", ephemeral=True)

class SmsModal(discord.ui.Modal, title="SMS Alert Settings"):
    enabled  = discord.ui.TextInput(label="Enable SMS? (true/false)", placeholder="false")
    sms_to   = discord.ui.TextInput(label="Phone gateway (number@carrier.com)", placeholder="4027180244@tmomail.net")
    gmail    = discord.ui.TextInput(label="Gmail address (sender)", placeholder="you@gmail.com")
    password = discord.ui.TextInput(label="Gmail App Password", placeholder="xxxx xxxx xxxx xxxx")
    events   = discord.ui.TextInput(
        label="Events to SMS (comma-separated)",
        placeholder="Tornado Emergency, Tornado Warning",
        style=discord.TextStyle.paragraph,
    )

    def __init__(self):
        super().__init__()
        self.enabled.default  = str(cfg.get("sms_enabled", False)).lower()
        self.sms_to.default   = cfg.get("sms_to", "4027180244@tmomail.net")
        self.gmail.default    = cfg.get("sms_gmail_address", "")
        self.password.default = cfg.get("sms_gmail_password", "")
        self.events.default   = ", ".join(cfg.get("sms_events", [
            "Tornado Emergency","Tornado Warning","Tornado Watch",
            "Severe Thunderstorm Warning","Severe Thunderstorm Watch"
        ]))

    async def on_submit(self, interaction: discord.Interaction):
        cfg["sms_enabled"]        = self.enabled.value.strip().lower() == "true"
        cfg["sms_to"]             = self.sms_to.value.strip()
        cfg["sms_gmail_address"]  = self.gmail.value.strip()
        cfg["sms_gmail_password"] = self.password.value.strip()
        cfg["sms_events"]         = [e.strip() for e in self.events.value.split(",") if e.strip()]
        save_config(cfg)
        status = "✅ enabled" if cfg["sms_enabled"] else "⏸️ disabled"
        await interaction.response.send_message(
            f"SMS alerts {status} → `{cfg['sms_to']}`\nEvents: {', '.join(cfg['sms_events'])}",
            ephemeral=True,
        )

# ── SMS Modal ─────────────────────────────────────────────────────────────────
class SmsModal(discord.ui.Modal, title="SMS Alert Settings"):
    enabled   = discord.ui.TextInput(label="Enable SMS? (true/false)", placeholder="false")
    number    = discord.ui.TextInput(label="Phone number (digits only, no +1)", placeholder="4027180244")
    carrier   = discord.ui.TextInput(label="Carrier (verizon/att/tmobile/sprint/boost)", placeholder="verizon")
    method    = discord.ui.TextInput(label="Method: api or smtp (api recommended)", placeholder="api")
    from_addr = discord.ui.TextInput(label="From address", placeholder="weather@oofbomb.xyz")

    def __init__(self):
        super().__init__()
        self.enabled.default   = str(cfg.get("sms_enabled", False)).lower()
        self.number.default    = cfg.get("sms_number", "4027180244")
        self.carrier.default   = cfg.get("sms_carrier", "verizon")
        self.method.default    = cfg.get("sms_method", "api")
        self.from_addr.default = cfg.get("sms_from", "weather@oofbomb.xyz")

    async def on_submit(self, interaction: discord.Interaction):
        cfg["sms_enabled"]   = self.enabled.value.strip().lower() == "true"
        cfg["sms_number"]    = self.number.value.strip().replace("-","").replace(" ","").replace("+1","")
        cfg["sms_carrier"]   = self.carrier.value.strip().lower()
        cfg["sms_method"]    = self.method.value.strip().lower() if self.method.value.strip().lower() in ("api","smtp") else "api"
        cfg["sms_from"]      = self.from_addr.value.strip()
        save_config(cfg)
        status  = "✅ SMS enabled" if cfg["sms_enabled"] else "⛔ SMS disabled"
        gateway = CARRIER_GATEWAYS.get(cfg["sms_carrier"], "@vtext.com")
        api_ok  = bool(cfg.get("sms_api_key") or os.getenv("BREVO_API_KEY"))
        smtp_ok = bool(cfg.get("sms_smtp_pass") or os.getenv("BREVO_SMTP_KEY"))
        key_status = "✅ API key set" if api_ok else "⚠️ No API key (set BREVO_API_KEY in .env)"
        await interaction.response.send_message(
            f"{status} · method: `{cfg['sms_method']}`\n"
            f"→ `{cfg['sms_number']}{gateway}`\n"
            f"From: `{cfg['sms_from']}`\n"
            f"Key: {key_status}\n"
            f"Events: {', '.join(cfg.get('sms_events', []))}",
            ephemeral=True,
        )

# ── Alert toggle select menu ──────────────────────────────────────────────────
class AlertToggleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        # Split alerts into two select menus (max 25 options each)
        all_alerts = list(cfg["enabled_alerts"].keys())
        half = len(all_alerts) // 2

        opts1 = [
            discord.SelectOption(
                label=k[:100],
                value=k,
                emoji=SEVERITY_EMOJI.get(k,"⚠️"),
                default=cfg["enabled_alerts"][k],
            ) for k in all_alerts[:half]
        ]
        opts2 = [
            discord.SelectOption(
                label=k[:100],
                value=k,
                emoji=SEVERITY_EMOJI.get(k,"⚠️"),
                default=cfg["enabled_alerts"][k],
            ) for k in all_alerts[half:]
        ]

        sel1 = discord.ui.Select(
            placeholder="Toggle alerts (part 1) — select to ENABLE",
            options=opts1, min_values=0, max_values=len(opts1),
        )
        sel2 = discord.ui.Select(
            placeholder="Toggle alerts (part 2) — select to ENABLE",
            options=opts2, min_values=0, max_values=len(opts2),
        )
        sel1.callback = self.make_callback(all_alerts[:half])
        sel2.callback = self.make_callback(all_alerts[half:])
        self.add_item(sel1)
        self.add_item(sel2)

    def make_callback(self, keys: list):
        async def callback(interaction: discord.Interaction):
            selected = set(interaction.data.get("values", []))
            for k in keys:
                cfg["enabled_alerts"][k] = k in selected
            save_config(cfg)
            on  = sum(1 for v in cfg["enabled_alerts"].values() if v)
            off = sum(1 for v in cfg["enabled_alerts"].values() if not v)
            await interaction.response.send_message(
                f"✅ Alert toggles saved — **{on}** enabled, **{off}** disabled.",
                ephemeral=True,
            )
        return callback

class ProductToggleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        opts = [
            discord.SelectOption(
                label=f"{k} — {self._label(k)}",
                value=k,
                default=cfg["iem_products"][k],
            ) for k in cfg["iem_products"]
        ]
        sel = discord.ui.Select(
            placeholder="Select products to ENABLE",
            options=opts, min_values=0, max_values=len(opts),
        )
        sel.callback = self.on_select
        self.add_item(sel)

    @staticmethod
    def _label(k):
        labels = {
            "AFD":"Area Forecast Discussion","HWO":"Hazardous Weather Outlook",
            "SPS":"Special Weather Statement","RVD":"River Forecast Discussion",
            "PNS":"Public Info Statement","LSR":"Local Storm Report",
            "SVR":"Severe T-Storm Warning","TOR":"Tornado Warning text",
            "FFW":"Flash Flood Warning","FWW":"Red Flag Warning","FFA":"Flash Flood Watch",
            "WOU":"Watch Outline Update",
        }
        return labels.get(k, k)

    async def on_select(self, interaction: discord.Interaction):
        selected = set(interaction.data.get("values", []))
        for k in cfg["iem_products"]:
            cfg["iem_products"][k] = k in selected
        save_config(cfg)
        on = [k for k,v in cfg["iem_products"].items() if v]
        await interaction.response.send_message(
            f"✅ Products saved — enabled: {', '.join(on) or 'none'}",
            ephemeral=True,
        )

# ── Slash commands ─────────────────────────────────────────────────────────────
@tree.command(name="settings", description="Configure WeatherWatch alerts, channels, and behavior")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_settings(interaction: discord.Interaction):
    view = SettingsView()
    await interaction.response.send_message(
        embed=view.settings_embed(),
        view=view,
        ephemeral=True,
    )

@tree.command(name="wx", description="Current NWS forecast for your configured location")
async def cmd_wx(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    async with aiohttp.ClientSession() as session:
        lat = cfg.get("lat", 41.26)
        lon = cfg.get("lon", -96.01)
        forecast = await fetch_point_forecast(session, lat, lon)
    emb = discord.Embed(
        title=f"🌤️ Forecast — {cfg.get('station','OAX')}",
        description=forecast[:3000],
        color=0x0099ff,
        timestamp=datetime.now(timezone.utc),
    )
    emb.set_footer(text=f"NWS · {lat},{lon}")
    await interaction.followup.send(embed=emb)

@tree.command(name="alerts", description="Show currently active NWS alerts for your zone")
async def cmd_alerts(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    async with aiohttp.ClientSession() as session:
        alerts = await fetch_active_alerts(session)

    if not alerts:
        await interaction.followup.send(
            embed=discord.Embed(title="✅ No active alerts", color=0x00cc66,
                description=f"No active NWS alerts for zone **{cfg.get('zone','NEZ040')}** / state **{cfg.get('state','NE')}**."))
        return

    # Show up to 5 active alerts
    for alert in alerts[:5]:
        emb = build_alert_embed(alert)
        await interaction.followup.send(embed=emb)

@tree.command(name="forecast", description="Get the latest Area Forecast Discussion from NWS")
async def cmd_forecast(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    async with aiohttp.ClientSession() as session:
        station = cfg.get("station","OAX")
        products = await fetch_iem_products(session, "AFD", station)
    if not products:
        await interaction.followup.send("No recent AFD found.")
        return
    emb = build_product_embed(products[0], "AFD")
    await interaction.followup.send(embed=emb)

@tree.command(name="hwo", description="Get the latest Hazardous Weather Outlook")
async def cmd_hwo(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    async with aiohttp.ClientSession() as session:
        products = await fetch_iem_products(session, "HWO", cfg.get("station","OAX"))
    if not products:
        await interaction.followup.send("No recent HWO found.")
        return
    emb = build_product_embed(products[0], "HWO")
    await interaction.followup.send(embed=emb)

@tree.command(name="poll", description="[Admin] Force an immediate alert poll")
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_poll(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        await check_alerts(session)
        await check_products(session)
    await interaction.followup.send("✅ Poll complete.", ephemeral=True)

@tree.command(name="wxstatus", description="Show bot status and current config summary")
async def cmd_status(interaction: discord.Interaction):
    ach = cfg.get("alert_channel_id", 0)
    pch = cfg.get("product_channel_id", 0)
    on_alerts   = sum(1 for v in cfg["enabled_alerts"].values() if v)
    on_products = sum(1 for v in cfg["iem_products"].values() if v)

    emb = discord.Embed(title="⛈️ WeatherWatch Status", color=0x0099ff,
                        timestamp=datetime.now(timezone.utc))
    emb.add_field(name="Station",  value=cfg.get("station","OAX"), inline=True)
    emb.add_field(name="Zone",     value=cfg.get("zone","—"),      inline=True)
    emb.add_field(name="State",    value=cfg.get("state","—"),     inline=True)
    emb.add_field(name="Alert Channel",   value=f"<#{ach}>" if ach else "Not set", inline=True)
    emb.add_field(name="Product Channel", value=f"<#{pch}>" if pch else "Not set", inline=True)
    emb.add_field(name="Poll Interval",   value=f"{cfg.get('poll_interval_secs',60)}s", inline=True)
    emb.add_field(name="Alerts Enabled",  value=str(on_alerts),   inline=True)
    emb.add_field(name="Products Enabled",value=str(on_products), inline=True)
    emb.add_field(name="Seen Alerts",     value=str(len(cfg["_seen_alerts"])), inline=True)
    await interaction.response.send_message(embed=emb, ephemeral=False)

# ── /test command ─────────────────────────────────────────────────────────────
TEST_EVENTS = {
    "tornado_emergency": {
        "event": "Tornado Emergency",
        "headline": "TORNADO EMERGENCY — Confirmed large and extremely dangerous tornado near Omaha moving northeast at 45 mph",
        "description": (
            "AT 3:47 PM CDT, a TORNADO EMERGENCY has been declared for a large, destructive tornado "
            "near Omaha. This is an EXTREMELY DANGEROUS AND LIFE-THREATENING SITUATION.\n\n"
            "TAKE COVER IMMEDIATELY in the lowest level of a sturdy building. Get under something "
            "sturdy and protect your head. DO NOT wait to see the tornado. Move now.\n\n"
            "This tornado is producing catastrophic damage. Entire neighborhoods have been destroyed "
            "along the tornado's path. Flying debris will be deadly to those caught without shelter."
        ),
        "instruction": "TAKE SHELTER IMMEDIATELY. Move to the lowest floor of a sturdy building. Avoid windows. Do not attempt to outrun this tornado by vehicle.",
        "severity": "Extreme",
        "urgency": "Immediate",
        "areaDesc": "Douglas [NE], Sarpy [NE], Washington [NE]",
        "senderName": "NWS Omaha/Valley NE",
    },
    "tornado_warning": {
        "event": "Tornado Warning",
        "headline": "Tornado Warning issued for Douglas County until 4:15 PM CDT",
        "description": (
            "AT 3:52 PM CDT, a severe thunderstorm capable of producing a tornado was located "
            "near Elkhorn, moving northeast at 35 mph.\n\n"
            "HAZARD: Tornado.\nSOURCE: Radar indicated rotation.\n"
            "IMPACT: Flying debris will be dangerous to those caught without shelter."
        ),
        "instruction": "Take cover now in a substantial shelter. If you are outdoors, in a mobile home, or in a vehicle, move to the closest substantial shelter and protect yourself from flying debris.",
        "severity": "Extreme",
        "urgency": "Immediate",
        "areaDesc": "Douglas [NE], Sarpy [NE]",
        "senderName": "NWS Omaha/Valley NE",
    },
    "severe_thunderstorm": {
        "event": "Severe Thunderstorm Warning",
        "headline": "Severe Thunderstorm Warning for Douglas County until 4:30 PM CDT",
        "description": (
            "AT 3:58 PM CDT, a severe thunderstorm was located near Omaha, moving northeast at 30 mph.\n\n"
            "HAZARD: Golf ball size hail and 70 mph wind gusts.\n"
            "SOURCE: Radar indicated.\n"
            "IMPACT: People and animals outdoors will be injured. Expect hail damage to roofs, "
            "siding, windows and vehicles. Expect wind damage to roofs, siding, and trees."
        ),
        "instruction": "Move to an interior room on the lowest floor of a sturdy building. Avoid windows.",
        "severity": "Severe",
        "urgency": "Immediate",
        "areaDesc": "Douglas [NE], Dodge [NE], Washington [NE]",
        "senderName": "NWS Omaha/Valley NE",
    },
    "flash_flood": {
        "event": "Flash Flood Warning",
        "headline": "Flash Flood Warning for Douglas County until 6:00 PM CDT",
        "description": (
            "AT 4:02 PM CDT, doppler radar indicated heavy rain due to thunderstorms. "
            "Flash flooding is ongoing or expected to begin shortly.\n\n"
            "HAZARD: Life threatening flash flooding. Heavy rain producing flash flooding.\n"
            "SOURCE: Doppler radar.\n"
            "IMPACT: Life threatening flash flooding of creeks and streams, urban areas, highways, "
            "streets and underpasses."
        ),
        "instruction": "Turn around, don't drown when encountering flooded roads. Most flood deaths occur in vehicles.",
        "severity": "Severe",
        "urgency": "Immediate",
        "areaDesc": "Douglas [NE], Sarpy [NE]",
        "senderName": "NWS Omaha/Valley NE",
    },
    "red_flag": {
        "event": "Red Flag Warning",
        "headline": "Red Flag Warning in effect until 8:00 PM CDT for critical fire weather conditions",
        "description": (
            "A Red Flag Warning means that critical fire weather conditions are either occurring "
            "or will occur shortly. A combination of strong winds, low relative humidity, and warm "
            "temperatures will create explosive fire growth potential.\n\n"
            "AFFECTED AREA: Fire weather zone 213 — Eastern Nebraska\n"
            "WINDS: Southwest 20 to 30 mph with gusts up to 45 mph.\n"
            "HUMIDITY: 10 to 15 percent.\n"
            "TEMPERATURES: Mid to upper 80s."
        ),
        "instruction": "Avoid outdoor burning. If a fire starts, it will likely spread rapidly. Report fires to local fire authorities immediately.",
        "severity": "Severe",
        "urgency": "Expected",
        "areaDesc": "Antelope [NE], Boone [NE], Burt [NE], Cedar [NE], Colfax [NE]",
        "senderName": "NWS Omaha/Valley NE",
    },
}

@tree.command(name="test", description="Send a realistic test alert with full ping config applied")
@app_commands.describe(
    event="Type of alert to test",
    silent="If true, no [TEST] label — looks completely real (default: False)",
    ping_override="Override ping: 'everyone', 'role', 'none', or a specific count like '5'",
    send_sms="Also send a real SMS text to your configured number (default: False)",
)
@app_commands.choices(event=[
    app_commands.Choice(name="🚨 Tornado Emergency (full @everyone spam)", value="tornado_emergency"),
    app_commands.Choice(name="🌪️ Tornado Warning",                         value="tornado_warning"),
    app_commands.Choice(name="⛈️ Severe Thunderstorm Warning",              value="severe_thunderstorm"),
    app_commands.Choice(name="🌊 Flash Flood Warning",                      value="flash_flood"),
    app_commands.Choice(name="🔥 Red Flag Warning",                         value="red_flag"),
])
@app_commands.checks.has_permissions(manage_guild=True)
async def cmd_test(
    interaction: discord.Interaction,
    event: str = "tornado_warning",
    silent: bool = False,
    ping_override: str = "",
    send_sms: bool = False,
):
    ch_id = cfg.get("alert_channel_id", 0)
    if not ch_id:
        await interaction.response.send_message("❌ No alert channel set — use `/settings` first.", ephemeral=True)
        return
    ch = bot.get_channel(ch_id)
    if not ch:
        await interaction.response.send_message("❌ Can't find alert channel. Check the ID in `/settings`.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    tdata      = TEST_EVENTS.get(event, TEST_EVENTS["tornado_warning"])
    event_name = tdata["event"]

    fake_alert = {
        "properties": {
            "id":          f"TEST_{event.upper()}_{int(datetime.now(timezone.utc).timestamp())}",
            "event":       event_name,
            "headline":    tdata["headline"],
            "description": tdata["description"],
            "instruction": tdata["instruction"],
            "severity":    tdata["severity"],
            "urgency":     tdata["urgency"],
            "onset":       datetime.now(timezone.utc).isoformat(),
            "expires":     (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "areaDesc":    tdata["areaDesc"],
            "senderName":  tdata["senderName"],
            "messageType": "Alert",
        }
    }

    emb = build_alert_embed(fake_alert)
    if not silent:
        emb.set_footer(text=f"⚠️ TEST ALERT · {tdata['senderName']} · WeatherWatch")
    else:
        emb.set_footer(text=f"{tdata['senderName']} · WeatherWatch")

    # ── Resolve ping ──────────────────────────────────────────────────────────
    # ping_override: "everyone", "role", "none", or a number like "3" (repeat count)
    is_everyone_event = event_name in cfg.get("everyone_events", [])
    role_id   = cfg.get("ping_role_id", 0)
    po        = ping_override.strip().lower() if ping_override else ""

    if po == "none":
        ping_str       = ""
        do_everyone    = False
        override_count = 0
    elif po == "everyone":
        ping_str       = "@everyone"
        do_everyone    = True
        override_count = cfg.get("tornado_everyone_count", 10)
    elif po == "role":
        ping_str       = f"<@&{role_id}>" if role_id else "@everyone"
        do_everyone    = False
        override_count = 0
    elif po.isdigit():
        # Numeric — treat as custom @everyone repeat count
        ping_str       = "@everyone"
        do_everyone    = True
        override_count = max(1, min(20, int(po)))
    else:
        # Default: use saved config
        if is_everyone_event:
            ping_str    = "@everyone"
            do_everyone = True
        elif role_id:
            ping_str    = f"<@&{role_id}>"
            do_everyone = False
        else:
            ping_str    = ""
            do_everyone = False
        override_count = cfg.get("tornado_everyone_count", 10)

    try:
        await ch.send(content=ping_str or None, embed=emb)
        log.info(f"Test alert sent: {event_name} to #{ch.name} | silent={silent} | ping={ping_str or 'none'}")

        # Optionally fire real SMS
        if send_sms:
            await send_sms_alert(event_name, tdata["headline"], tdata["areaDesc"])
            log.info(f"Test SMS fired for {event_name}")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to send: {e}", ephemeral=True)
        return

    # ── Repeat pings ──────────────────────────────────────────────────────────
    if do_everyone and override_count > 1:
        delay = cfg.get("tornado_everyone_delay", 2)
        areas = tdata["areaDesc"]
        label = "" if silent else " `[TEST]`"
        await interaction.followup.send(
            f"✅ Sent to <#{ch_id}>. Running **{override_count}x** @everyone with **{delay}s** delay…",
            ephemeral=True,
        )
        for i in range(override_count - 1):
            await asyncio.sleep(delay)
            try:
                await ch.send(
                    f"🚨 **{event_name.upper()}** 🚨 @everyone — {areas[:100]} ({i+2}/{override_count}){label}"
                )
            except Exception:
                pass
    else:
        ping_info = f"Pinged: `{ping_str}`" if ping_str else "No ping"
        await interaction.followup.send(
            f"✅ **{event_name}** sent to <#{ch_id}> · {ping_info} · silent={silent}",
            ephemeral=True,
        )

# ── Error handler ─────────────────────────────────────────────────────────────
@tree.error
async def on_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Manage Server** permission.", ephemeral=True)
    else:
        log.error(f"Command error: {error}")
        try:
            await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
        except Exception:
            pass

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("ERROR: Set WEATHERWATCH_TOKEN in your .env file")
        print("  cp .env.example .env && edit .env")
        exit(1)
    bot.run(BOT_TOKEN)