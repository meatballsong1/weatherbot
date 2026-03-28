"""
WeatherWatch Web Panel
======================
Flask web panel for managing the WeatherWatch Discord bot.
Runs on port 5000 (proxy via nginx to weather.oofbomb.xyz).

Dependencies:
    pip install flask python-dotenv

Run:
    python panel.py
"""

import json
import os
import subprocess
import signal
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("PANEL_SECRET_KEY", "dev-change-me-in-production")

PANEL_USER = os.getenv("PANEL_USERNAME", "oofbomb")
PANEL_PASS = os.getenv("PANEL_PASSWORD", "malaop0989")
PANEL_PORT = int(os.getenv("PANEL_PORT", 5000))
CONFIG_FILE = Path("weatherwatch_config.json")

BOT_PROCESS = None  # track subprocess

DEFAULT_CONFIG = {
    "station": "OAX", "zone": "NEZ040", "state": "NE",
    "lat": 41.26, "lon": -96.01,
    "alert_channel_id": 0, "product_channel_id": 0,
    "ping_role_id": 0, "poll_interval_secs": 60,
    "everyone_events": ["Tornado Emergency", "Tornado Warning", "Tornado Watch"],
    "tornado_everyone_count": 10, "tornado_everyone_delay": 2,
    "embed_style": "rich",
    "show_affected_areas": True, "show_expiry": True,
    "post_all_clear": True, "show_source": True,
    "enabled_alerts": {
        "Tornado Emergency": True, "Tornado Warning": True,
        "Tornado Watch": True, "Severe Thunderstorm Warning": True,
        "Severe Thunderstorm Watch": True, "Flash Flood Emergency": True,
        "Flash Flood Warning": True, "Flash Flood Watch": True,
        "Red Flag Warning": True, "Fire Weather Watch": True,
        "Winter Storm Warning": True, "Winter Storm Watch": True,
        "Blizzard Warning": True, "Ice Storm Warning": True,
        "Hazardous Weather Outlook": False, "Area Forecast Discussion": False,
        "Special Weather Statement": True, "Dust Storm Warning": True,
        "Extreme Cold Warning": True, "Extreme Heat Warning": True,
        "Dense Fog Advisory": False, "Wind Advisory": False,
        "High Wind Warning": True, "High Wind Watch": True,
    },
    "iem_products": {
        "AFD": False, "HWO": False, "SPS": True, "RVD": False,
        "PNS": False, "LSR": True, "SVR": True, "TOR": True,
        "FFW": True, "FWW": True, "FFA": False, "WOU": False,
    },
    "_seen_alerts": [], "_seen_products": [],
}

ALERT_LABELS = {
    "AFD": "Area Forecast Discussion", "HWO": "Hazardous Weather Outlook",
    "SPS": "Special Weather Statement", "RVD": "River Forecast Discussion",
    "PNS": "Public Information Statement", "LSR": "Local Storm Report",
    "SVR": "Severe T-Storm Warning text", "TOR": "Tornado Warning text",
    "FFW": "Flash Flood Warning text", "FWW": "Red Flag Warning text",
    "FFA": "Flash Flood Watch text", "WOU": "Watch Outline Update",
}

ALERT_EMOJI = {
    "Tornado Emergency": "🚨", "Tornado Warning": "🌪️", "Tornado Watch": "⚠️",
    "Severe Thunderstorm Warning": "⛈️", "Severe Thunderstorm Watch": "🌩️",
    "Flash Flood Emergency": "🚨", "Flash Flood Warning": "🌊", "Flash Flood Watch": "💧",
    "Red Flag Warning": "🔥", "Fire Weather Watch": "🔥",
    "Winter Storm Warning": "🌨️", "Winter Storm Watch": "❄️",
    "Blizzard Warning": "🌬️", "Ice Storm Warning": "🧊",
    "High Wind Warning": "💨", "High Wind Watch": "💨",
    "Special Weather Statement": "📋", "Hazardous Weather Outlook": "📋",
    "Area Forecast Discussion": "📄", "Dust Storm Warning": "🌫️",
    "Extreme Cold Warning": "🥶", "Extreme Heat Warning": "🥵",
    "Dense Fog Advisory": "🌫️", "Wind Advisory": "💨",
}

def load_cfg():
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(saved)
            for k in ("enabled_alerts", "iem_products"):
                if k in saved:
                    cfg[k] = {**DEFAULT_CONFIG[k], **saved[k]}
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_cfg(cfg):
    out = dict(cfg)
    out["_seen_alerts"]   = cfg.get("_seen_alerts", [])[-500:]
    out["_seen_products"] = cfg.get("_seen_products", [])[-500:]
    CONFIG_FILE.write_text(json.dumps(out, indent=2))

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

def bot_status():
    global BOT_PROCESS
    if BOT_PROCESS and BOT_PROCESS.poll() is None:
        return "running"
    # Also check via pgrep
    result = subprocess.run(["pgrep", "-f", "weather_bot.py"], capture_output=True, text=True)
    if result.returncode == 0:
        return "running"
    return "stopped"

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET"])
def login_page():
    if session.get("logged_in"):
        return redirect(url_for("dashboard"))
    return render_template_string(LOGIN_HTML)

@app.route("/api/login", methods=["POST"])
def do_login():
    data = request.get_json() or {}
    if data.get("username") == PANEL_USER and data.get("password") == PANEL_PASS:
        session["logged_in"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Invalid credentials"}), 401

@app.route("/api/logout", methods=["POST"])
def do_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/")
@login_required
def dashboard():
    return render_template_string(DASHBOARD_HTML)

@app.route("/api/config", methods=["GET"])
@login_required
def get_config():
    cfg = load_cfg()
    # Don't send seen lists to frontend
    out = {k: v for k, v in cfg.items() if not k.startswith("_")}
    return jsonify(out)

@app.route("/api/config", methods=["POST"])
@login_required
def update_config():
    cfg = load_cfg()
    data = request.get_json() or {}
    scalar_keys = [
        "station","zone","state","lat","lon",
        "alert_channel_id","product_channel_id","ping_role_id",
        "poll_interval_secs","tornado_everyone_count","tornado_everyone_delay",
        "embed_style","show_affected_areas","show_expiry","post_all_clear","show_source",
    ]
    for k in scalar_keys:
        if k in data:
            cfg[k] = data[k]
    if "enabled_alerts" in data:
        cfg["enabled_alerts"].update(data["enabled_alerts"])
    if "iem_products" in data:
        cfg["iem_products"].update(data["iem_products"])
    if "everyone_events" in data:
        cfg["everyone_events"] = data["everyone_events"]
    save_cfg(cfg)
    return jsonify({"ok": True})

@app.route("/api/bot/status", methods=["GET"])
@login_required
def get_bot_status():
    status = bot_status()
    cfg = load_cfg()
    return jsonify({
        "status": status,
        "station": cfg.get("station","OAX"),
        "seen_alerts": len(cfg.get("_seen_alerts",[])),
        "seen_products": len(cfg.get("_seen_products",[])),
        "alert_channel": cfg.get("alert_channel_id",0),
        "product_channel": cfg.get("product_channel_id",0),
    })

@app.route("/api/bot/start", methods=["POST"])
@login_required
def start_bot():
    global BOT_PROCESS
    if bot_status() == "running":
        return jsonify({"ok": False, "error": "Bot is already running"})
    try:
        BOT_PROCESS = subprocess.Popen(
            ["python3", "weather_bot.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({"ok": True, "pid": BOT_PROCESS.pid})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/bot/stop", methods=["POST"])
@login_required
def stop_bot():
    global BOT_PROCESS
    # Kill our tracked process
    if BOT_PROCESS and BOT_PROCESS.poll() is None:
        BOT_PROCESS.terminate()
        try: BOT_PROCESS.wait(timeout=5)
        except: BOT_PROCESS.kill()
        BOT_PROCESS = None
    # Also kill any orphaned processes
    subprocess.run(["pkill", "-f", "weather_bot.py"], capture_output=True)
    return jsonify({"ok": True})

@app.route("/api/bot/restart", methods=["POST"])
@login_required
def restart_bot():
    stop_bot()
    import time; time.sleep(1)
    return start_bot()

@app.route("/api/seen/clear", methods=["POST"])
@login_required
def clear_seen():
    cfg = load_cfg()
    cfg["_seen_alerts"]   = []
    cfg["_seen_products"] = []
    save_cfg(cfg)
    return jsonify({"ok": True})

@app.route("/api/logs", methods=["GET"])
@login_required
def get_logs():
    log_file = Path("weatherwatch.log")
    if not log_file.exists():
        return jsonify({"lines": []})
    lines = log_file.read_text().splitlines()[-100:]
    return jsonify({"lines": lines})

# ══════════════════════════════════════════════════════════════════════════════
#  LOGIN HTML
# ══════════════════════════════════════════════════════════════════════════════
LOGIN_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>weather bot · login</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#08070f;--s1:#0f0d1a;--s2:#16132a;
  --border:rgba(139,92,246,0.15);--border2:rgba(139,92,246,0.3);
  --text:#e8e4ff;--muted:#6b5fa8;--dim:#2a2540;
  --v:#8b5cf6;--v2:#a78bfa;--v3:#c4b5fd;
  --glow:rgba(139,92,246,0.4);
  --sans:'Syne',sans-serif;--mono:'JetBrains Mono',monospace;
}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:var(--sans);overflow:hidden}

/* Animated background */
.bg-fx{position:fixed;inset:0;z-index:0;pointer-events:none}
.orb{position:absolute;border-radius:50%;filter:blur(80px);animation:drift 12s ease-in-out infinite}
.orb1{width:500px;height:500px;background:radial-gradient(circle,rgba(139,92,246,0.18),transparent 70%);top:-150px;left:-100px;animation-delay:0s}
.orb2{width:400px;height:400px;background:radial-gradient(circle,rgba(124,58,237,0.12),transparent 70%);bottom:-100px;right:-80px;animation-delay:-6s}
.orb3{width:300px;height:300px;background:radial-gradient(circle,rgba(167,139,250,0.08),transparent 70%);top:40%;left:40%;animation-delay:-3s}
@keyframes drift{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(30px,-20px) scale(1.05)}66%{transform:translate(-20px,30px) scale(0.95)}}

/* Grid */
.grid-bg{position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:linear-gradient(rgba(139,92,246,0.04) 1px,transparent 1px),linear-gradient(90deg,rgba(139,92,246,0.04) 1px,transparent 1px);
  background-size:48px 48px}

.wrap{position:relative;z-index:1;height:100%;display:flex;align-items:center;justify-content:center;padding:20px}

.card{
  background:rgba(15,13,26,0.85);backdrop-filter:blur(20px);
  border:1px solid var(--border2);border-radius:20px;
  padding:44px 40px;width:100%;max-width:380px;
  box-shadow:0 0 0 1px rgba(139,92,246,0.1),0 32px 64px rgba(0,0,0,0.6),0 0 80px rgba(139,92,246,0.08);
  animation:cardIn .6s cubic-bezier(.34,1.3,.64,1) both;
}
@keyframes cardIn{from{opacity:0;transform:translateY(24px) scale(.97)}to{opacity:1;transform:none}}

.brand{text-align:center;margin-bottom:32px}
.brand-icon{
  width:52px;height:52px;border-radius:14px;margin:0 auto 12px;
  background:linear-gradient(135deg,var(--v),#4c1d95);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 0 32px var(--glow);
  font-size:24px;
}
.brand-name{font-size:22px;font-weight:800;letter-spacing:-.5px}
.brand-name span{color:var(--v2)}
.brand-sub{font-size:12px;color:var(--muted);margin-top:4px;font-family:var(--mono)}

.flabel{font-size:11px;font-weight:600;color:var(--muted);margin-bottom:6px;letter-spacing:.05em;display:block}
.iw{position:relative;margin-bottom:14px}
.iw svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);width:15px;height:15px;color:var(--muted);pointer-events:none}
input{
  width:100%;padding:11px 12px 11px 38px;
  background:rgba(255,255,255,0.04);border:1.5px solid var(--border);
  border-radius:10px;color:var(--text);font-family:var(--sans);font-size:14px;
  outline:none;transition:border-color .18s,box-shadow .18s;
}
input:focus{border-color:var(--v);box-shadow:0 0 0 3px rgba(139,92,246,0.2)}
input::placeholder{color:var(--muted)}

.btn-login{
  width:100%;padding:13px;border:none;border-radius:10px;
  background:linear-gradient(135deg,var(--v),#4c1d95);
  color:#fff;font-family:var(--sans);font-size:15px;font-weight:700;
  cursor:pointer;transition:transform .15s,box-shadow .15s;
  box-shadow:0 4px 20px rgba(139,92,246,0.4);margin-top:4px;
}
.btn-login:hover{transform:translateY(-2px);box-shadow:0 8px 28px rgba(139,92,246,0.5)}
.btn-login:active{transform:scale(.97)}
.btn-login:disabled{opacity:.6;cursor:default;transform:none}

.err{
  display:none;margin-top:10px;padding:9px 12px;
  background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);
  border-radius:8px;font-size:12.5px;color:#f87171;text-align:center;
}

.footer-txt{text-align:center;margin-top:20px;font-size:11px;color:var(--muted);font-family:var(--mono)}
.footer-txt a{color:var(--v2);text-decoration:none}
</style>
</head>
<body>
<div class="bg-fx">
  <div class="orb orb1"></div><div class="orb orb2"></div><div class="orb orb3"></div>
</div>
<div class="grid-bg"></div>
<div class="wrap">
  <div class="card">
    <div class="brand">
      <div class="brand-icon">⛈️</div>
      <div class="brand-name">weather<span>bot</span></div>
      <div class="brand-sub">control panel · weather.oofbomb.xyz</div>
    </div>
    <label class="flabel">username</label>
    <div class="iw">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
      <input type="text" id="uIn" placeholder="oofbomb" onkeydown="if(event.key==='Enter')doLogin()">
    </div>
    <label class="flabel">password</label>
    <div class="iw">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      <input type="password" id="pIn" placeholder="••••••••" onkeydown="if(event.key==='Enter')doLogin()">
    </div>
    <button class="btn-login" id="loginBtn" onclick="doLogin()">sign in</button>
    <div class="err" id="errMsg"></div>
    <div class="footer-txt">made by <a href="#">oofbomb</a></div>
  </div>
</div>
<script>
async function doLogin() {
  const btn = document.getElementById('loginBtn');
  const err = document.getElementById('errMsg');
  const u   = document.getElementById('uIn').value.trim();
  const p   = document.getElementById('pIn').value;
  if (!u || !p) { showErr('enter username and password'); return; }
  btn.disabled = true; btn.textContent = 'signing in…';
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username: u, password: p})
    });
    const d = await r.json();
    if (d.ok) { window.location.href = '/'; }
    else { showErr(d.error || 'invalid credentials'); btn.disabled=false; btn.textContent='sign in'; }
  } catch(e) { showErr('connection error'); btn.disabled=false; btn.textContent='sign in'; }
}
function showErr(msg) {
  const el = document.getElementById('errMsg');
  el.style.display = 'block'; el.textContent = msg;
  setTimeout(()=>el.style.display='none', 3000);
}
</script>
</body>
</html>'''

# ══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD HTML
# ══════════════════════════════════════════════════════════════════════════════
DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>weather bot · panel</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#08070f;--s1:#0f0d1a;--s2:#16132a;--s3:#1e1a35;
  --border:rgba(139,92,246,0.12);--border2:rgba(139,92,246,0.25);
  --text:#e8e4ff;--text2:#9b8ec4;--muted:#6b5fa8;--dim:#2a2540;
  --v:#8b5cf6;--v2:#a78bfa;--v3:#c4b5fd;
  --green:#10b981;--greeng:rgba(16,185,129,0.12);
  --red:#ef4444;--redg:rgba(239,68,68,0.10);
  --yellow:#f59e0b;--yellowg:rgba(245,158,11,0.10);
  --glow:rgba(139,92,246,0.35);
  --sans:'Syne',sans-serif;--mono:'JetBrains Mono',monospace;
}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;-webkit-font-smoothing:antialiased;overflow-x:hidden}

/* BG */
.bg-fx{position:fixed;inset:0;z-index:0;pointer-events:none}
.orb{position:absolute;border-radius:50%;filter:blur(100px);animation:drift 14s ease-in-out infinite}
.orb1{width:600px;height:600px;background:radial-gradient(circle,rgba(139,92,246,0.12),transparent 70%);top:-200px;left:-150px}
.orb2{width:500px;height:500px;background:radial-gradient(circle,rgba(76,29,149,0.10),transparent 70%);bottom:-150px;right:-100px;animation-delay:-7s}
@keyframes drift{0%,100%{transform:translate(0,0)}50%{transform:translate(40px,-30px)}}
.grid-bg{position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:linear-gradient(rgba(139,92,246,0.035) 1px,transparent 1px),linear-gradient(90deg,rgba(139,92,246,0.035) 1px,transparent 1px);
  background-size:52px 52px}

/* Layout */
.wrap{position:relative;z-index:1;max-width:1020px;margin:0 auto;padding:20px 18px 60px}

/* Topbar */
.topbar{
  display:flex;align-items:center;justify-content:space-between;
  background:rgba(15,13,26,0.8);backdrop-filter:blur(20px);
  border:1px solid var(--border2);border-radius:16px;
  padding:12px 18px;margin-bottom:22px;
  box-shadow:0 0 40px rgba(139,92,246,0.06);
  position:sticky;top:12px;z-index:50;
}
.tb-brand{display:flex;align-items:center;gap:10px}
.tb-icon{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--v),#4c1d95);display:flex;align-items:center;justify-content:center;font-size:16px;box-shadow:0 0 20px var(--glow)}
.tb-name{font-size:16px;font-weight:800;letter-spacing:-.3px}
.tb-name span{color:var(--v2)}
.tb-right{display:flex;align-items:center;gap:8px}

/* Status pill */
.spill{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:20px;font-size:11.5px;font-weight:700;transition:all .3s}
.spill.running{color:var(--green);background:var(--greeng);border:1px solid rgba(16,185,129,0.2)}
.spill.stopped{color:var(--red);background:var(--redg);border:1px solid rgba(239,68,68,0.2)}
.spill.checking{color:var(--muted);background:rgba(139,92,246,0.08);border:1px solid var(--border)}
.sdot{width:6px;height:6px;border-radius:50%;background:currentColor}
.spill.running .sdot{animation:pdot 2s infinite}
@keyframes pdot{0%,100%{box-shadow:0 0 0 0 rgba(16,185,129,.5)}60%{box-shadow:0 0 0 5px rgba(16,185,129,0)}}

/* Bot controls */
.ctrl-row{display:flex;gap:7px}
.btn{display:inline-flex;align-items:center;gap:5px;font-family:var(--sans);font-size:12.5px;font-weight:700;border:none;cursor:pointer;border-radius:8px;padding:7px 13px;transition:transform .14s,opacity .14s,box-shadow .14s;white-space:nowrap;user-select:none}
.btn:active{transform:scale(.96)!important}
.btn-v{background:linear-gradient(135deg,var(--v),#4c1d95);color:#fff;box-shadow:0 3px 14px var(--glow)}
.btn-v:hover{transform:translateY(-1px);box-shadow:0 6px 20px var(--glow)}
.btn-green{background:var(--greeng);color:var(--green);border:1px solid rgba(16,185,129,.2)}
.btn-green:hover{background:rgba(16,185,129,.2);transform:translateY(-1px)}
.btn-red{background:var(--redg);color:var(--red);border:1px solid rgba(239,68,68,.15)}
.btn-red:hover{background:rgba(239,68,68,.18)}
.btn-yellow{background:var(--yellowg);color:var(--yellow);border:1px solid rgba(245,158,11,.18)}
.btn-yellow:hover{background:rgba(245,158,11,.2);transform:translateY(-1px)}
.btn-ghost{background:transparent;color:var(--text2);border:1px solid var(--border)}
.btn-ghost:hover{background:rgba(139,92,246,.08);color:var(--v2);border-color:var(--border2)}
.btn-sm{font-size:11px;padding:5px 10px;border-radius:7px}
.btn:disabled{opacity:.5;cursor:default;transform:none!important}

/* Cards */
.card{background:rgba(15,13,26,0.75);backdrop-filter:blur(16px);border:1px solid var(--border);border-radius:16px;padding:20px;margin-bottom:14px;transition:border-color .2s}
.card:hover{border-color:var(--border2)}
.card-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px}
.card-title{font-size:14px;font-weight:800;letter-spacing:-.2px}
.card-sub{font-size:11px;color:var(--muted);margin-top:1px}
.card-actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}

/* 2-col */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
@media(max-width:600px){.g2{grid-template-columns:1fr}}

/* Stat blocks */
.stat{background:rgba(22,19,42,0.8);border:1px solid var(--border);border-radius:12px;padding:14px 16px;transition:border-color .2s}
.stat:hover{border-color:var(--border2)}
.stat-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:5px}
.stat-val{font-size:30px;font-weight:800;letter-spacing:-1.2px;font-variant-numeric:tabular-nums}
.c-v{color:var(--v2)} .c-g{color:var(--green)} .c-y{color:var(--yellow)} .c-r{color:var(--red)}

/* Form inputs */
.flabel{font-size:11px;font-weight:700;color:var(--muted);margin-bottom:5px;letter-spacing:.05em;display:block}
.iw{position:relative;margin-bottom:12px}
.iw svg{position:absolute;left:11px;top:50%;transform:translateY(-50%);width:14px;height:14px;color:var(--muted);pointer-events:none}
input[type=text],input[type=number],select{
  width:100%;padding:9px 11px 9px 34px;
  background:rgba(255,255,255,0.04);border:1.5px solid var(--border);
  border-radius:9px;color:var(--text);font-family:var(--sans);font-size:13px;
  outline:none;transition:border-color .18s,box-shadow .18s;
}
input[type=number]{padding-left:11px}
select{padding-left:11px;cursor:pointer}
input:focus,select:focus{border-color:var(--v);box-shadow:0 0 0 3px rgba(139,92,246,0.15)}
input::placeholder{color:var(--muted)}

/* Toggle grid */
.toggle-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px}
.toggle-row{display:flex;align-items:center;justify-content:space-between;padding:8px 11px;background:rgba(22,19,42,0.6);border:1px solid var(--border);border-radius:9px;transition:border-color .15s}
.toggle-row:hover{border-color:var(--border2)}
.toggle-label{font-size:12.5px;font-weight:600;display:flex;align-items:center;gap:6px}
.pill{width:36px;height:20px;border-radius:10px;background:var(--dim);border:none;cursor:pointer;position:relative;transition:background .2s;flex-shrink:0}
.pill.on{background:var(--v)}
.pill::after{content:'';position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;transition:transform .2s cubic-bezier(.34,1.4,.64,1);box-shadow:0 1px 4px rgba(0,0,0,.3)}
.pill.on::after{transform:translateX(16px)}

/* Save bar */
.save-bar{display:flex;justify-content:flex-end;gap:8px;padding-top:12px;border-top:1px solid var(--border);margin-top:12px}

/* Tabs */
.tabs{display:flex;gap:5px;margin-bottom:14px;flex-wrap:wrap}
.tab{padding:6px 14px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--muted);transition:all .15s;font-family:var(--sans)}
.tab.active{background:rgba(139,92,246,0.12);color:var(--v2);border-color:rgba(139,92,246,.25)}

/* Log panel */
.log-box{background:rgba(8,7,15,0.9);border:1px solid var(--border);border-radius:10px;padding:12px 14px;font-family:var(--mono);font-size:11.5px;line-height:1.7;max-height:280px;overflow-y:auto;color:var(--text2)}
.log-box::-webkit-scrollbar{width:3px}
.log-box::-webkit-scrollbar-thumb{background:var(--dim);border-radius:2px}
.log-line.err{color:#f87171}
.log-line.warn{color:var(--yellow)}
.log-line.ok{color:var(--green)}

/* Toast */
#toasts{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:6px;pointer-events:none}
.toast{display:flex;align-items:center;gap:8px;padding:10px 14px;background:rgba(15,13,26,0.95);border:1px solid var(--border2);border-radius:9px;font-size:13px;font-weight:600;animation:tIn .25s cubic-bezier(.34,1.3,.64,1) both;pointer-events:auto;max-width:300px}
.toast.out{animation:tOut .2s ease forwards}
@keyframes tIn{from{opacity:0;transform:translateX(16px) scale(.96)}to{opacity:1;transform:none}}
@keyframes tOut{to{opacity:0;transform:translateX(10px) scale(.96)}}
.t-ok{border-color:rgba(16,185,129,.25);color:var(--green)}
.t-err{border-color:rgba(239,68,68,.25);color:var(--red)}
.t-info{border-color:rgba(139,92,246,.25);color:var(--v2)}

/* Range slider */
input[type=range]{padding:0;background:none;border:none;box-shadow:none;accent-color:var(--v);width:100%}

/* Divider */
.div{height:1px;background:var(--border);margin:12px 0 16px}

/* Footer */
.footer{text-align:center;padding-top:24px;font-size:11px;color:var(--muted);font-family:var(--mono)}
.footer a{color:var(--v2);text-decoration:none}
</style>
</head>
<body>

<div class="bg-fx"><div class="orb orb1"></div><div class="orb orb2"></div></div>
<div class="grid-bg"></div>

<div class="wrap">

  <!-- Topbar -->
  <div class="topbar">
    <div class="tb-brand">
      <div class="tb-icon">⛈️</div>
      <div class="tb-name">weather<span>bot</span></div>
    </div>
    <div class="tb-right">
      <div id="statusPill" class="spill checking"><div class="sdot"></div><span id="statusTxt">checking…</span></div>
      <div class="ctrl-row">
        <button class="btn btn-green btn-sm" onclick="botAction('start')" id="btnStart">▶ start</button>
        <button class="btn btn-red btn-sm"   onclick="botAction('stop')"  id="btnStop">■ stop</button>
        <button class="btn btn-yellow btn-sm" onclick="botAction('restart')" id="btnRestart">↺ restart</button>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="doLogout()">sign out</button>
    </div>
  </div>

  <!-- Stat row -->
  <div class="g2">
    <div class="stat"><div class="stat-label">Alerts Seen</div><div class="stat-val c-v" id="statSeen">—</div><div style="font-size:11px;color:var(--muted);margin-top:3px">deduplicated</div></div>
    <div class="stat"><div class="stat-label">Products Seen</div><div class="stat-val c-g" id="statProds">—</div><div style="font-size:11px;color:var(--muted);margin-top:3px">IEM text products</div></div>
  </div>

  <!-- ── TABS ───────────────────────────────────────────── -->
  <div class="tabs">
    <button class="tab active" onclick="showTab('station')">📡 station</button>
    <button class="tab" onclick="showTab('channels')">📢 channels</button>
    <button class="tab" onclick="showTab('alerts')">🚨 alerts</button>
    <button class="tab" onclick="showTab('products')">📄 products</button>
    <button class="tab" onclick="showTab('tornado')">🌪️ tornado</button>
    <button class="tab" onclick="showTab('behavior')">⚙️ behavior</button>
    <button class="tab" onclick="showTab('logs')">📋 logs</button>
  </div>

  <!-- STATION TAB -->
  <div class="card" id="tab-station">
    <div class="card-hdr"><div><div class="card-title">📡 station & location</div><div class="card-sub">NWS office, zone, and coordinates</div></div></div>
    <div class="g2">
      <div>
        <label class="flabel">NWS Station ID</label>
        <div class="iw"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M4.93 4.93a10 10 0 0 0 0 14.14"/></svg>
        <input type="text" id="cfg-station" placeholder="OAX" style="text-transform:uppercase"></div>
      </div>
      <div>
        <label class="flabel">NWS Zone (e.g. NEZ040)</label>
        <div class="iw"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
        <input type="text" id="cfg-zone" placeholder="NEZ040"></div>
      </div>
      <div>
        <label class="flabel">State (2-letter)</label>
        <div class="iw"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/></svg>
        <input type="text" id="cfg-state" placeholder="NE" maxlength="2" style="text-transform:uppercase"></div>
      </div>
      <div>
        <label class="flabel">Latitude</label>
        <input type="number" id="cfg-lat" placeholder="41.26" step="0.01">
      </div>
      <div>
        <label class="flabel">Longitude</label>
        <input type="number" id="cfg-lon" placeholder="-96.01" step="0.01">
      </div>
    </div>
    <div class="save-bar"><button class="btn btn-ghost btn-sm" onclick="loadConfig()">↺ reload</button><button class="btn btn-v btn-sm" onclick="saveSection('station')">save station</button></div>
  </div>

  <!-- CHANNELS TAB -->
  <div class="card" id="tab-channels" style="display:none">
    <div class="card-hdr"><div><div class="card-title">📢 discord channels</div><div class="card-sub">where alerts and products get posted</div></div></div>
    <div class="g2">
      <div>
        <label class="flabel">Alert Channel ID</label>
        <input type="number" id="cfg-alert_channel_id" placeholder="123456789012345678">
      </div>
      <div>
        <label class="flabel">Products Channel ID</label>
        <input type="number" id="cfg-product_channel_id" placeholder="123456789012345678">
      </div>
      <div>
        <label class="flabel">Ping Role ID (for significant alerts)</label>
        <input type="number" id="cfg-ping_role_id" placeholder="leave blank for none">
      </div>
      <div>
        <label class="flabel">Poll Interval (seconds, min 30)</label>
        <input type="number" id="cfg-poll_interval_secs" placeholder="60" min="30">
      </div>
    </div>
    <div class="save-bar"><button class="btn btn-ghost btn-sm" onclick="loadConfig()">↺ reload</button><button class="btn btn-v btn-sm" onclick="saveSection('channels')">save channels</button></div>
  </div>

  <!-- ALERTS TAB -->
  <div class="card" id="tab-alerts" style="display:none">
    <div class="card-hdr">
      <div><div class="card-title">🚨 alert toggles</div><div class="card-sub">which NWS event types trigger a Discord post</div></div>
      <div class="card-actions">
        <button class="btn btn-ghost btn-sm" onclick="setAllAlerts(true)">enable all</button>
        <button class="btn btn-ghost btn-sm" onclick="setAllAlerts(false)">disable all</button>
      </div>
    </div>
    <div class="toggle-grid" id="alertToggles"></div>
    <div class="save-bar"><button class="btn btn-v btn-sm" onclick="saveSection('alerts')">save alerts</button></div>
  </div>

  <!-- PRODUCTS TAB -->
  <div class="card" id="tab-products" style="display:none">
    <div class="card-hdr">
      <div><div class="card-title">📄 iem text products</div><div class="card-sub">NWS office text products via Iowa Environmental Mesonet (all off by default)</div></div>
      <div class="card-actions">
        <button class="btn btn-ghost btn-sm" onclick="setAllProducts(true)">enable all</button>
        <button class="btn btn-ghost btn-sm" onclick="setAllProducts(false)">disable all</button>
      </div>
    </div>
    <div class="toggle-grid" id="productToggles"></div>
    <div class="save-bar"><button class="btn btn-v btn-sm" onclick="saveSection('products')">save products</button></div>
  </div>

  <!-- TORNADO TAB -->
  <div class="card" id="tab-tornado" style="display:none">
    <div class="card-hdr"><div><div class="card-title">🌪️ tornado emergency config</div><div class="card-sub">@everyone ping behavior for extreme events</div></div></div>
    <div class="g2">
      <div>
        <label class="flabel">@everyone ping count per Tornado Emergency</label>
        <input type="number" id="cfg-tornado_everyone_count" min="1" max="20" placeholder="10">
      </div>
      <div>
        <label class="flabel">Seconds between @everyone pings</label>
        <input type="number" id="cfg-tornado_everyone_delay" min="1" max="30" placeholder="2">
      </div>
    </div>
    <label class="flabel">events that trigger @everyone (comma-separated)</label>
    <input type="text" id="cfg-everyone_events_str" placeholder="Tornado Emergency, Tornado Warning, Tornado Watch" style="margin-bottom:12px">
    <div style="padding:10px 12px;background:rgba(239,68,68,0.06);border:1px solid rgba(239,68,68,0.15);border-radius:9px;font-size:12px;color:#f87171">
      ⚠️ Tornado Emergency will ping @everyone <span id="tornadoCountDisplay">10</span>x with <span id="tornadoDelayDisplay">2</span>s between each ping.
    </div>
    <div class="save-bar"><button class="btn btn-v btn-sm" onclick="saveSection('tornado')">save tornado config</button></div>
  </div>

  <!-- BEHAVIOR TAB -->
  <div class="card" id="tab-behavior" style="display:none">
    <div class="card-hdr"><div><div class="card-title">⚙️ behavior</div><div class="card-sub">embed style, display options, all-clear</div></div></div>
    <div class="g2">
      <div>
        <label class="flabel">Embed style</label>
        <select id="cfg-embed_style">
          <option value="rich">rich (full details)</option>
          <option value="compact">compact</option>
          <option value="minimal">minimal (title only)</option>
        </select>
      </div>
    </div>
    <div class="div"></div>
    <div class="toggle-grid">
      <div class="toggle-row"><span class="toggle-label">Show affected areas</span><button class="pill" id="pill-show_affected_areas" onclick="togglePill('show_affected_areas')"></button></div>
      <div class="toggle-row"><span class="toggle-label">Show expiry time</span><button class="pill" id="pill-show_expiry" onclick="togglePill('show_expiry')"></button></div>
      <div class="toggle-row"><span class="toggle-label">Post all-clear when expires</span><button class="pill" id="pill-post_all_clear" onclick="togglePill('post_all_clear')"></button></div>
      <div class="toggle-row"><span class="toggle-label">Show source (NWS office)</span><button class="pill" id="pill-show_source" onclick="togglePill('show_source')"></button></div>
    </div>
    <div class="save-bar">
      <button class="btn btn-ghost btn-sm" onclick="clearSeen()">🔄 clear seen history</button>
      <button class="btn btn-v btn-sm" onclick="saveSection('behavior')">save behavior</button>
    </div>
  </div>

  <!-- LOGS TAB -->
  <div class="card" id="tab-logs" style="display:none">
    <div class="card-hdr">
      <div><div class="card-title">📋 logs</div><div class="card-sub">last 100 lines from weatherwatch.log</div></div>
      <div class="card-actions">
        <button class="btn btn-ghost btn-sm" onclick="loadLogs()">↺ refresh</button>
        <button class="btn btn-ghost btn-sm" id="autoRefreshBtn" onclick="toggleAutoRefresh()">auto: off</button>
      </div>
    </div>
    <div class="log-box" id="logBox">loading…</div>
  </div>

  <div class="footer">made by <a href="#">oofbomb</a> · weather bot control panel</div>
</div>

<div id="toasts"></div>

<script>
let cfg = {};
let behaviorPills = {show_affected_areas:true,show_expiry:true,post_all_clear:true,show_source:true};
let autoRefreshIv = null;

const ALERT_EMOJI = {
  "Tornado Emergency":"🚨","Tornado Warning":"🌪️","Tornado Watch":"⚠️",
  "Severe Thunderstorm Warning":"⛈️","Severe Thunderstorm Watch":"🌩️",
  "Flash Flood Emergency":"🚨","Flash Flood Warning":"🌊","Flash Flood Watch":"💧",
  "Red Flag Warning":"🔥","Fire Weather Watch":"🔥",
  "Winter Storm Warning":"🌨️","Winter Storm Watch":"❄️",
  "Blizzard Warning":"🌬️","Ice Storm Warning":"🧊",
  "High Wind Warning":"💨","High Wind Watch":"💨",
  "Special Weather Statement":"📋","Hazardous Weather Outlook":"📋",
  "Area Forecast Discussion":"📄","Dust Storm Warning":"🌫️",
  "Extreme Cold Warning":"🥶","Extreme Heat Warning":"🥵",
  "Dense Fog Advisory":"🌫️","Wind Advisory":"💨",
};
const PROD_LABELS = {
  AFD:"Area Forecast Discussion",HWO:"Hazardous Weather Outlook",
  SPS:"Special Weather Statement",RVD:"River Forecast Discussion",
  PNS:"Public Information Statement",LSR:"Local Storm Report",
  SVR:"Severe T-Storm Warning text",TOR:"Tornado Warning text",
  FFW:"Flash Flood Warning text",FWW:"Red Flag Warning text",
  FFA:"Flash Flood Watch text",WOU:"Watch Outline Update",
};

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadConfig();
  pollStatus();
  setInterval(pollStatus, 5000);
});

// ── Status ────────────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const r = await fetch('/api/bot/status');
    const d = await r.json();
    const pill = document.getElementById('statusPill');
    const txt  = document.getElementById('statusTxt');
    pill.className = 'spill ' + d.status;
    txt.textContent = d.status;
    document.getElementById('statSeen').textContent  = d.seen_alerts  ?? '—';
    document.getElementById('statProds').textContent = d.seen_products ?? '—';
  } catch {}
}

// ── Bot controls ──────────────────────────────────────────────────────────────
async function botAction(action) {
  const btn = document.getElementById('btn' + action.charAt(0).toUpperCase() + action.slice(1));
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const r = await fetch('/api/bot/' + action, {method:'POST'});
    const d = await r.json();
    if (d.ok) toast(action + ' successful', 'ok');
    else toast(d.error || action + ' failed', 'err');
  } catch(e) { toast('error: ' + e.message, 'err'); }
  if (btn) { btn.disabled=false; btn.textContent = {start:'▶ start',stop:'■ stop',restart:'↺ restart'}[action]; }
  setTimeout(pollStatus, 1500);
}

// ── Config load/save ──────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    cfg = await r.json();
    applyConfig(cfg);
    toast('config loaded', 'info');
  } catch(e) { toast('failed to load config', 'err'); }
}

function applyConfig(c) {
  // Station
  setVal('cfg-station',  c.station);
  setVal('cfg-zone',     c.zone);
  setVal('cfg-state',    c.state);
  setVal('cfg-lat',      c.lat);
  setVal('cfg-lon',      c.lon);

  // Channels
  setVal('cfg-alert_channel_id',   c.alert_channel_id   || '');
  setVal('cfg-product_channel_id', c.product_channel_id || '');
  setVal('cfg-ping_role_id',       c.ping_role_id       || '');
  setVal('cfg-poll_interval_secs', c.poll_interval_secs || 60);

  // Tornado
  setVal('cfg-tornado_everyone_count', c.tornado_everyone_count || 10);
  setVal('cfg-tornado_everyone_delay', c.tornado_everyone_delay || 2);
  setVal('cfg-everyone_events_str', (c.everyone_events || []).join(', '));
  updateTornadoDisplay();

  // Behavior
  setVal('cfg-embed_style', c.embed_style || 'rich');
  ['show_affected_areas','show_expiry','post_all_clear','show_source'].forEach(k => {
    behaviorPills[k] = c[k] !== undefined ? c[k] : true;
    const p = document.getElementById('pill-' + k);
    if (p) p.classList.toggle('on', behaviorPills[k]);
  });

  // Alert toggles
  renderAlertToggles(c.enabled_alerts || {});
  renderProductToggles(c.iem_products || {});
}

function setVal(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val ?? '';
}

function getVal(id) {
  const el = document.getElementById(id);
  return el ? el.value : '';
}

function updateTornadoDisplay() {
  const c = parseInt(getVal('cfg-tornado_everyone_count')) || 10;
  const d = parseInt(getVal('cfg-tornado_everyone_delay')) || 2;
  document.getElementById('tornadoCountDisplay').textContent = c;
  document.getElementById('tornadoDelayDisplay').textContent = d;
}
document.addEventListener('input', e => {
  if (e.target.id && e.target.id.includes('tornado')) updateTornadoDisplay();
});

// ── Alert / product toggle rendering ─────────────────────────────────────────
function renderAlertToggles(alerts) {
  const c = document.getElementById('alertToggles');
  c.innerHTML = '';
  Object.entries(alerts).forEach(([k, v]) => {
    const em = ALERT_EMOJI[k] || '⚠️';
    const row = document.createElement('div');
    row.className = 'toggle-row';
    row.innerHTML = `
      <span class="toggle-label">${em} ${k}</span>
      <button class="pill${v?' on':''}" id="alert-pill-${encodeURIComponent(k)}" onclick="toggleAlertPill('${k}')"></button>
    `;
    c.appendChild(row);
  });
}

function renderProductToggles(prods) {
  const c = document.getElementById('productToggles');
  c.innerHTML = '';
  Object.entries(prods).forEach(([k, v]) => {
    const label = PROD_LABELS[k] || k;
    const row = document.createElement('div');
    row.className = 'toggle-row';
    row.innerHTML = `
      <span class="toggle-label"><code style="font-family:var(--mono);font-size:11px;background:rgba(139,92,246,0.1);padding:1px 5px;border-radius:4px;color:var(--v2)">${k}</code> ${label}</span>
      <button class="pill${v?' on':''}" id="prod-pill-${k}" onclick="toggleProductPill('${k}')"></button>
    `;
    c.appendChild(row);
  });
}

function toggleAlertPill(key) {
  const id = 'alert-pill-' + encodeURIComponent(key);
  const p  = document.getElementById(id);
  if (!p) return;
  const on = p.classList.toggle('on');
  if (!cfg.enabled_alerts) cfg.enabled_alerts = {};
  cfg.enabled_alerts[key] = on;
}

function toggleProductPill(key) {
  const p = document.getElementById('prod-pill-' + key);
  if (!p) return;
  const on = p.classList.toggle('on');
  if (!cfg.iem_products) cfg.iem_products = {};
  cfg.iem_products[key] = on;
}

function setAllAlerts(val) {
  Object.keys(cfg.enabled_alerts || {}).forEach(k => {
    cfg.enabled_alerts[k] = val;
    const p = document.getElementById('alert-pill-' + encodeURIComponent(k));
    if (p) p.classList.toggle('on', val);
  });
}
function setAllProducts(val) {
  Object.keys(cfg.iem_products || {}).forEach(k => {
    cfg.iem_products[k] = val;
    const p = document.getElementById('prod-pill-' + k);
    if (p) p.classList.toggle('on', val);
  });
}

function togglePill(key) {
  behaviorPills[key] = !behaviorPills[key];
  const p = document.getElementById('pill-' + key);
  if (p) p.classList.toggle('on', behaviorPills[key]);
}

// ── Save sections ─────────────────────────────────────────────────────────────
async function saveSection(section) {
  let payload = {};
  switch(section) {
    case 'station':
      payload = {
        station: getVal('cfg-station').toUpperCase(),
        zone:    getVal('cfg-zone').toUpperCase(),
        state:   getVal('cfg-state').toUpperCase(),
        lat:     parseFloat(getVal('cfg-lat')) || 0,
        lon:     parseFloat(getVal('cfg-lon')) || 0,
      }; break;
    case 'channels':
      payload = {
        alert_channel_id:   parseInt(getVal('cfg-alert_channel_id'))   || 0,
        product_channel_id: parseInt(getVal('cfg-product_channel_id')) || 0,
        ping_role_id:       parseInt(getVal('cfg-ping_role_id'))       || 0,
        poll_interval_secs: Math.max(30, parseInt(getVal('cfg-poll_interval_secs')) || 60),
      }; break;
    case 'alerts':
      payload = {enabled_alerts: cfg.enabled_alerts}; break;
    case 'products':
      payload = {iem_products: cfg.iem_products}; break;
    case 'tornado':
      payload = {
        tornado_everyone_count: Math.max(1, Math.min(20, parseInt(getVal('cfg-tornado_everyone_count')) || 10)),
        tornado_everyone_delay: Math.max(1, parseInt(getVal('cfg-tornado_everyone_delay')) || 2),
        everyone_events: getVal('cfg-everyone_events_str').split(',').map(s=>s.trim()).filter(Boolean),
      }; break;
    case 'behavior':
      payload = {
        embed_style: getVal('cfg-embed_style'),
        ...Object.fromEntries(Object.entries(behaviorPills)),
      }; break;
  }
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.ok) toast('saved ✓', 'ok');
    else toast('save failed', 'err');
  } catch(e) { toast('error: ' + e.message, 'err'); }
}

async function clearSeen() {
  const r = await fetch('/api/seen/clear', {method:'POST'});
  const d = await r.json();
  if (d.ok) { toast('seen history cleared', 'ok'); pollStatus(); }
  else toast('failed', 'err');
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
const TABS = ['station','channels','alerts','products','tornado','behavior','logs'];
function showTab(name) {
  TABS.forEach(t => {
    document.getElementById('tab-' + t).style.display = t===name ? '' : 'none';
  });
  document.querySelectorAll('.tab').forEach((btn,i) => {
    btn.classList.toggle('active', TABS[i] === name);
  });
  if (name === 'logs') loadLogs();
}

// ── Logs ──────────────────────────────────────────────────────────────────────
async function loadLogs() {
  const box = document.getElementById('logBox');
  try {
    const r = await fetch('/api/logs');
    const d = await r.json();
    box.innerHTML = '';
    if (!d.lines.length) { box.textContent = 'no log data yet'; return; }
    d.lines.forEach(line => {
      const el = document.createElement('div');
      el.className = 'log-line' + (line.includes('ERROR') ? ' err' : line.includes('WARNING') ? ' warn' : line.includes('INFO') ? ' ok' : '');
      el.textContent = line;
      box.appendChild(el);
    });
    box.scrollTop = box.scrollHeight;
  } catch(e) { box.textContent = 'failed to load logs'; }
}

function toggleAutoRefresh() {
  const btn = document.getElementById('autoRefreshBtn');
  if (autoRefreshIv) {
    clearInterval(autoRefreshIv);
    autoRefreshIv = null;
    btn.textContent = 'auto: off';
  } else {
    autoRefreshIv = setInterval(loadLogs, 3000);
    btn.textContent = 'auto: on';
    loadLogs();
  }
}

// ── Logout ────────────────────────────────────────────────────────────────────
async function doLogout() {
  await fetch('/api/logout', {method:'POST'});
  window.location.href = '/login';
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type='info') {
  const el = document.createElement('div');
  el.className = `toast t-${type}`;
  el.textContent = msg;
  el.onclick = () => { el.classList.add('out'); setTimeout(()=>el.remove(),200); };
  document.getElementById('toasts').appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(()=>el.remove(),200); }, 3200);
}
</script>
</body>
</html>'''

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PANEL_PORT, debug=False)