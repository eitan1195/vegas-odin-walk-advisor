#!/usr/bin/env python3
"""
Vegas & Odin Walk Advisor.
Pulls live Bangkok weather + air-quality from Open-Meteo (no key needed)
and scores how good a walk is for two golden retrievers right now.
"""

import json
import mimetypes
import os
import socket
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

BANGKOK_LAT = 13.7563
BANGKOK_LON = 100.5018
# Honor $PORT for cloud hosts (Render, Fly, Railway, Hugging Face all set it).
PORT = int(os.environ.get("PORT", "8080"))

# In-memory cache of the last successful advice payload so a transient upstream
# failure doesn't take the whole app down with a 502.
_LAST_OK = {"data": None, "ts": 0.0}
# How old a cached payload may be before we stop serving it as a fallback.
STALE_MAX_AGE_SEC = 60 * 60  # 1 hour

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={BANGKOK_LAT}&longitude={BANGKOK_LON}"
    "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
    "precipitation,weather_code,wind_speed_10m,uv_index,is_day"
    "&timezone=Asia%2FBangkok"
)
AIR_URL = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    f"?latitude={BANGKOK_LAT}&longitude={BANGKOK_LON}"
    "&current=pm2_5,pm10,us_aqi,ozone,nitrogen_dioxide"
    "&timezone=Asia%2FBangkok"
)


def fetch_json(url, timeout=6, retries=2):
    req = urllib.request.Request(url, headers={"User-Agent": "walk-advisor/1.0"})
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
    raise last


# Score each factor 0..100 (100 = ideal). Lower scores cap walk time more.
def score_heat(apparent_c):
    # Goldens are double-coated; heat is the killer. Based on apparent temp (heat index).
    if apparent_c <= 22:
        return 100
    if apparent_c <= 26:
        return 85
    if apparent_c <= 29:
        return 65
    if apparent_c <= 32:
        return 40
    if apparent_c <= 35:
        return 20
    if apparent_c <= 38:
        return 8
    return 2  # >38C: emergency-only potty break


def score_aqi(us_aqi):
    if us_aqi is None:
        return 60
    if us_aqi <= 50:
        return 100  # good
    if us_aqi <= 100:
        return 75  # moderate
    if us_aqi <= 150:
        return 45  # unhealthy for sensitive (active dogs count)
    if us_aqi <= 200:
        return 20  # unhealthy
    if us_aqi <= 300:
        return 8
    return 2


def score_rain(precip_mm, weather_code):
    # Thunderstorm codes: 95-99. Heavy rain codes: 65,67,82.
    if weather_code in (95, 96, 99):
        return 5  # thunder = stay inside
    if precip_mm >= 4:
        return 25
    if precip_mm >= 1:
        return 60  # light rain, can be cooling
    return 100


def score_uv(uv, is_day):
    if not is_day:
        return 100
    if uv is None:
        return 75
    if uv < 3:
        return 100
    if uv < 6:
        return 85
    if uv < 8:
        return 65
    if uv < 11:
        return 45
    return 25


WEATHER_LABELS = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm w/ hail", 99: "thunderstorm w/ hail",
}


def compute_advice():
    w = fetch_json(WEATHER_URL)["current"]
    # Air quality API is flakier; degrade gracefully if it fails.
    try:
        a = fetch_json(AIR_URL)["current"]
        air_ok = True
    except Exception as e:
        print(f"  air-quality fetch failed: {e}")
        a = {}
        air_ok = False

    apparent = w["apparent_temperature"]
    temp = w["temperature_2m"]
    humidity = w["relative_humidity_2m"]
    precip = w.get("precipitation", 0) or 0
    code = w.get("weather_code", 0)
    wind = w.get("wind_speed_10m", 0)
    uv = w.get("uv_index")
    is_day = bool(w.get("is_day", 1))
    aqi = a.get("us_aqi")
    pm25 = a.get("pm2_5")
    pm10 = a.get("pm10")

    s_heat = score_heat(apparent)
    s_air = score_aqi(aqi)
    s_rain = score_rain(precip, code)
    s_uv = score_uv(uv, is_day)

    # Weighted: heat dominates for goldens in Bangkok, air quality second.
    weighted = s_heat * 0.45 + s_air * 0.30 + s_rain * 0.15 + s_uv * 0.10
    # But: any single critical factor caps the verdict. A "perfect AQI" doesn't
    # rescue a dangerous heat index for double-coated dogs.
    cap = min(s_heat + 15, s_air + 15, s_rain + 10, s_uv + 20)
    overall = round(min(weighted, cap))

    # Max walk minutes: derived from the worst factor, capped by heat & air.
    # Base: 60 min at perfect, 0 min if anything is critical.
    heat_minutes = {100: 75, 85: 60, 65: 35, 40: 20, 20: 10, 8: 5, 2: 0}[s_heat]
    air_minutes = {100: 75, 75: 60, 45: 30, 20: 12, 8: 5, 2: 0}[s_air]
    rain_minutes = 999 if s_rain >= 60 else (15 if s_rain >= 25 else 0)
    max_minutes = max(0, min(heat_minutes, air_minutes, rain_minutes))

    if overall >= 80:
        verdict = "GO"
        verdict_color = "#1b8a3a"
        msg = "Great conditions. Vegas and Odin will love it."
    elif overall >= 60:
        verdict = "OK"
        verdict_color = "#b58a00"
        msg = "Decent. Keep it moderate, bring water, watch for panting."
    elif overall >= 40:
        verdict = "SHORT"
        verdict_color = "#c2570a"
        msg = "Marginal. Quick walk only, shaded route, lots of water."
    elif overall >= 20:
        verdict = "POTTY ONLY"
        verdict_color = "#b8261c"
        msg = "Bad. Out for bathroom break, straight back in."
    else:
        verdict = "STAY IN"
        verdict_color = "#7a0d0d"
        msg = "Dangerous. Indoor play and AC instead."

    notes = []
    if apparent >= 32:
        notes.append(f"Feels like {apparent:.1f}C - paw-test the pavement (5-sec rule).")
    if humidity >= 70 and apparent >= 28:
        notes.append("High humidity makes panting much less effective at cooling.")
    if aqi is not None and aqi > 100:
        notes.append(f"AQI {int(aqi)} - active dogs breathe more; consider skipping or short.")
    if pm25 is not None and pm25 > 35:
        notes.append(f"PM2.5 {pm25:.0f} ug/m3 is above WHO 24h guideline.")
    if code in (95, 96, 99):
        notes.append("Thunderstorm risk - lightning is fatal in open areas.")
    if uv and is_day and uv >= 8:
        notes.append(f"UV {uv:.1f} - very high; avoid midday sun on light-coated coats.")
    if not is_day:
        notes.append("Night walk - cooler, easier on the dogs.")
    if not air_ok:
        notes.append("Air-quality data unavailable right now - score based on weather only.")

    return {
        "verdict": verdict,
        "verdict_color": verdict_color,
        "message": msg,
        "overall_score": overall,
        "max_walk_minutes": max_minutes,
        "factors": {
            "heat": s_heat,
            "air": s_air,
            "rain": s_rain,
            "uv": s_uv,
        },
        "raw": {
            "temperature_c": temp,
            "apparent_c": apparent,
            "humidity_pct": humidity,
            "precipitation_mm": precip,
            "weather": WEATHER_LABELS.get(code, f"code {code}"),
            "wind_kmh": wind,
            "uv_index": uv,
            "is_day": is_day,
            "us_aqi": aqi,
            "pm2_5": pm25,
            "pm10": pm10,
        },
        "notes": notes,
        "stale": False,
        "age_seconds": 0,
    }


def get_advice():
    """Compute fresh advice, falling back to the last good payload on failure."""
    try:
        data = compute_advice()
        _LAST_OK["data"] = data
        _LAST_OK["ts"] = time.time()
        return data
    except Exception as e:
        cached = _LAST_OK["data"]
        age = time.time() - _LAST_OK["ts"] if cached else None
        if cached and age is not None and age <= STALE_MAX_AGE_SEC:
            stale = dict(cached)
            stale["stale"] = True
            stale["age_seconds"] = int(age)
            stale["notes"] = list(cached.get("notes", [])) + [
                f"Live data unavailable ({type(e).__name__}); showing reading from "
                f"{int(age // 60)} min ago."
            ]
            return stale
        raise


DOGS_SVG = """
<svg viewBox="0 0 220 110" xmlns="http://www.w3.org/2000/svg" aria-label="Two golden retrievers">
  <defs>
    <radialGradient id="bg" cx="50%" cy="40%" r="70%">
      <stop offset="0%" stop-color="#fff4dd"/>
      <stop offset="100%" stop-color="#f5d99a"/>
    </radialGradient>
  </defs>
  <rect width="220" height="110" rx="20" fill="url(#bg)"/>
  <!-- left dog: Vegas (lighter) -->
  <g transform="translate(72,62)">
    <ellipse cx="-23" cy="2" rx="11" ry="24" fill="#b8772e" transform="rotate(-18 -23 2)"/>
    <ellipse cx="23"  cy="2" rx="11" ry="24" fill="#b8772e" transform="rotate(18 23 2)"/>
    <circle cx="0" cy="0" r="28" fill="#ecbf72"/>
    <ellipse cx="0" cy="12" rx="14" ry="11" fill="#f6d496"/>
    <ellipse cx="0" cy="7" rx="3.5" ry="2.5" fill="#1a1a1a"/>
    <circle cx="-10" cy="-6" r="3" fill="#1a1a1a"/>
    <circle cx="10"  cy="-6" r="3" fill="#1a1a1a"/>
    <circle cx="-9"  cy="-7" r="1" fill="#fff"/>
    <circle cx="11"  cy="-7" r="1" fill="#fff"/>
    <path d="M-6 14 Q0 19 6 14" stroke="#222" stroke-width="1.8" fill="none" stroke-linecap="round"/>
  </g>
  <!-- right dog: Odin (darker, tongue out) -->
  <g transform="translate(150,62)">
    <ellipse cx="-23" cy="2" rx="11" ry="24" fill="#955d18" transform="rotate(-18 -23 2)"/>
    <ellipse cx="23"  cy="2" rx="11" ry="24" fill="#955d18" transform="rotate(18 23 2)"/>
    <circle cx="0" cy="0" r="28" fill="#d99a4a"/>
    <ellipse cx="0" cy="12" rx="14" ry="11" fill="#eab062"/>
    <ellipse cx="0" cy="7" rx="3.5" ry="2.5" fill="#1a1a1a"/>
    <circle cx="-10" cy="-6" r="3" fill="#1a1a1a"/>
    <circle cx="10"  cy="-6" r="3" fill="#1a1a1a"/>
    <circle cx="-9"  cy="-7" r="1" fill="#fff"/>
    <circle cx="11"  cy="-7" r="1" fill="#fff"/>
    <path d="M-6 13 Q0 17 6 13 L4 22 Q0 25 -4 22 Z" fill="#ef7a8a"/>
    <path d="M0 17 L0 23" stroke="#c75a6c" stroke-width="0.8"/>
  </g>
</svg>
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f8a13a">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Vegas &amp; Odin</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f6f1e7;
    --card: #ffffff;
    --text: #1a1a1a;
    --muted: #6b7280;
    --line: #efe8d8;
    --accent: #f59f3c;
    --accent-dark: #c97a18;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #15120e;
      --card: #201c16;
      --text: #f0eadc;
      --muted: #9c948a;
      --line: #2e2820;
      --accent: #f8a13a;
      --accent-dark: #b87311;
    }
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Segoe UI", system-ui, sans-serif;
    max-width: 460px; margin: 0 auto; padding: 18px 16px 40px;
    padding-top: max(18px, env(safe-area-inset-top));
    padding-bottom: max(40px, env(safe-area-inset-bottom));
    -webkit-font-smoothing: antialiased;
  }
  .hero {
    background: linear-gradient(135deg, #ffd58a 0%, #f8a13a 60%, #e0701c 100%);
    border-radius: 22px; padding: 18px 18px 20px; color: #2a1900;
    box-shadow: 0 10px 30px rgba(224,112,28,0.25);
    margin-bottom: 16px; position: relative; overflow: hidden;
  }
  .hero-top { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
  .hero-title { font-size: 13px; font-weight: 600; letter-spacing: 0.6px; text-transform: uppercase; opacity: 0.75; }
  .hero-sub { font-size: 12px; opacity: 0.7; }
  .dogs-img {
    width: 100%; max-height: 160px; display: block;
    border-radius: 16px; object-fit: cover; margin-bottom: 14px;
    background: #fff4dd;
  }
  .names { display: flex; justify-content: space-around; font-weight: 700; font-size: 14px; margin-top: -8px; margin-bottom: 12px; opacity: 0.85; }
  .verdict-row { display: flex; align-items: center; gap: 14px; }
  .ring-wrap { position: relative; width: 96px; height: 96px; flex-shrink: 0; }
  .ring-wrap svg { transform: rotate(-90deg); }
  .ring-bg { stroke: rgba(255,255,255,0.35); }
  .ring-fg { stroke: #ffffff; stroke-linecap: round; transition: stroke-dashoffset 0.6s ease; }
  .ring-text {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center; line-height: 1;
  }
  .ring-text .num { font-size: 28px; font-weight: 800; }
  .ring-text .lbl { font-size: 10px; opacity: 0.75; margin-top: 3px; letter-spacing: 0.5px; }
  .verdict-info { flex: 1; min-width: 0; }
  .verdict-badge {
    display: inline-block; background: rgba(0,0,0,0.18); color: #fff;
    font-weight: 800; font-size: 16px; letter-spacing: 0.5px;
    padding: 6px 12px; border-radius: 10px; margin-bottom: 6px;
  }
  .max-walk { font-size: 22px; font-weight: 800; line-height: 1.1; }
  .max-walk small { font-size: 12px; font-weight: 500; opacity: 0.7; display: block; margin-top: 2px; }
  .msg { font-size: 13.5px; margin-top: 10px; opacity: 0.85; line-height: 1.35; }

  .card {
    background: var(--card); border-radius: 18px; padding: 16px;
    margin-bottom: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.04);
  }
  .card h2 {
    margin: 0 0 12px; font-size: 13px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted);
  }
  .factor {
    display: flex; align-items: center; gap: 10px; padding: 9px 0;
    border-top: 1px solid var(--line);
  }
  .factor:first-of-type { border-top: none; padding-top: 2px; }
  .factor-icon { font-size: 20px; width: 28px; text-align: center; }
  .factor-label { font-weight: 600; font-size: 14px; flex-shrink: 0; min-width: 48px; }
  .bar { flex: 1; height: 8px; background: var(--line); border-radius: 99px; overflow: hidden; }
  .bar > div { height: 100%; border-radius: 99px; transition: width 0.6s ease; }
  .factor-num { font-weight: 700; font-size: 13px; min-width: 28px; text-align: right; }

  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .stat {
    background: var(--bg); border-radius: 12px; padding: 10px 12px;
  }
  .stat .k { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .stat .v { font-size: 17px; font-weight: 700; margin-top: 2px; }
  .stat .v small { font-size: 11px; font-weight: 500; opacity: 0.6; }

  ul.notes { padding: 0; margin: 0; list-style: none; }
  ul.notes li {
    background: var(--bg); border-radius: 10px; padding: 9px 12px;
    margin-bottom: 6px; font-size: 13.5px; line-height: 1.35;
    border-left: 3px solid var(--accent);
  }
  ul.notes li:last-child { margin-bottom: 0; }
  .no-notes { color: var(--muted); font-size: 13px; }

  .refresh-btn {
    width: 100%; background: var(--accent); color: #2a1900;
    border: none; padding: 14px; border-radius: 14px;
    font-size: 15px; font-weight: 700; cursor: pointer;
    box-shadow: 0 4px 14px rgba(245,159,60,0.35);
    margin-top: 4px;
  }
  .refresh-btn:active { transform: scale(0.98); }
  .meta {
    font-size: 11px; color: var(--muted); margin-top: 14px;
    text-align: center; line-height: 1.5;
  }
  .skeleton {
    background: linear-gradient(90deg, var(--line) 0%, var(--bg) 50%, var(--line) 100%);
    background-size: 200% 100%; animation: shimmer 1.4s infinite;
    border-radius: 18px; height: 280px; margin-bottom: 12px;
  }
  @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
  .err { color: #b8261c; padding: 14px; background: var(--card); border-radius: 12px; }
  .pulse { animation: pulse 2s infinite; }
  @keyframes pulse { 50% { opacity: 0.6; } }
</style>
</head>
<body>
  <div id="root">
    <div class="skeleton"></div>
    <div class="skeleton" style="height:140px"></div>
  </div>

<script>
const DOGS_SVG_FALLBACK = `__DOGS_SVG__`;

function barColor(score) {
  if (score >= 80) return "#1b8a3a";
  if (score >= 60) return "#d4a017";
  if (score >= 40) return "#e07b1c";
  if (score >= 20) return "#c43a2e";
  return "#7a0d0d";
}

function ring(score) {
  const R = 42, C = 2 * Math.PI * R;
  const off = C * (1 - score / 100);
  return `
    <div class="ring-wrap">
      <svg width="96" height="96" viewBox="0 0 96 96">
        <circle class="ring-bg" cx="48" cy="48" r="${R}" stroke-width="8" fill="none"/>
        <circle class="ring-fg" cx="48" cy="48" r="${R}" stroke-width="8" fill="none"
                stroke-dasharray="${C}" stroke-dashoffset="${off}"/>
      </svg>
      <div class="ring-text"><div class="num">${score}</div><div class="lbl">SCORE</div></div>
    </div>`;
}

async function checkPhoto() {
  // Use user-supplied photo if dropped at /static/dogs.{jpg,png,webp}
  for (const ext of ["jpg","jpeg","png","webp"]) {
    try {
      const r = await fetch(`/static/dogs.${ext}`, { method: "HEAD" });
      if (r.ok) return `/static/dogs.${ext}`;
    } catch (e) {}
  }
  return null;
}

function render(d, photoUrl) {
  const f = d.factors, r = d.raw;
  const factor = (icon, label, score) => `
    <div class="factor">
      <div class="factor-icon">${icon}</div>
      <div class="factor-label">${label}</div>
      <div class="bar"><div style="width:${score}%;background:${barColor(score)}"></div></div>
      <div class="factor-num">${score}</div>
    </div>`;
  const notesHtml = d.notes.length
    ? `<ul class="notes">${d.notes.map(n => `<li>${n}</li>`).join("")}</ul>`
    : `<div class="no-notes">No special warnings right now.</div>`;

  const dogImg = photoUrl
    ? `<img class="dogs-img" src="${photoUrl}" alt="Vegas and Odin">`
    : `<div class="dogs-img" style="padding:8px">${DOGS_SVG_FALLBACK}</div>`;

  document.getElementById("root").innerHTML = `
    <div class="hero">
      <div class="hero-top">
        <div style="flex:1">
          <div class="hero-title">Walk Advisor</div>
          <div class="hero-sub">Bangkok - live</div>
        </div>
      </div>
      ${dogImg}
      <div class="names"><span>Vegas</span><span>Odin</span></div>
      <div class="verdict-row">
        ${ring(d.overall_score)}
        <div class="verdict-info">
          <div class="verdict-badge">${d.verdict}</div>
          <div class="max-walk">${d.max_walk_minutes} min<small>max walk</small></div>
        </div>
      </div>
      <div class="msg">${d.message}</div>
    </div>

    <div class="card">
      <h2>Factors</h2>
      ${factor("&#x2600;&#xfe0f;", "Heat", f.heat)}
      ${factor("&#x1f343;", "Air",  f.air)}
      ${factor("&#x1f327;&#xfe0f;", "Rain", f.rain)}
      ${factor("&#x1f576;&#xfe0f;", "UV",   f.uv)}
    </div>

    <div class="card">
      <h2>Conditions now</h2>
      <div class="stats">
        <div class="stat"><div class="k">Temp</div><div class="v">${r.temperature_c.toFixed(1)}<small> C</small></div></div>
        <div class="stat"><div class="k">Feels like</div><div class="v">${r.apparent_c.toFixed(1)}<small> C</small></div></div>
        <div class="stat"><div class="k">Humidity</div><div class="v">${r.humidity_pct}<small> %</small></div></div>
        <div class="stat"><div class="k">US AQI</div><div class="v">${r.us_aqi ?? "-"}</div></div>
        <div class="stat"><div class="k">PM2.5</div><div class="v">${r.pm2_5 ?? "-"}<small> ug/m3</small></div></div>
        <div class="stat"><div class="k">UV index</div><div class="v">${r.uv_index ?? "-"}</div></div>
        <div class="stat"><div class="k">Sky</div><div class="v" style="font-size:14px">${r.weather}</div></div>
        <div class="stat"><div class="k">Wind</div><div class="v">${r.wind_kmh}<small> km/h</small></div></div>
      </div>
    </div>

    <div class="card">
      <h2>Notes</h2>
      ${notesHtml}
    </div>

    <button class="refresh-btn" onclick="load()">Refresh now</button>
    <div class="meta">Data: Open-Meteo. Goldens are double-coated &amp; heat-sensitive.<br>Paw-test pavement (5-sec rule) before every walk.</div>
  `;
}

async function load() {
  try {
    const [res, photoUrl] = await Promise.all([
      fetch("/api/walk?_=" + Date.now()),
      checkPhoto(),
    ]);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const d = await res.json();
    render(d, photoUrl);
  } catch (e) {
    document.getElementById("root").innerHTML =
      `<div class="err">Failed to load: ${e.message}<br><br>
       <button class="refresh-btn" onclick="load()">Try again</button></div>`;
  }
}
load();
setInterval(load, 5 * 60 * 1000);
</script>
</body>
</html>
""".replace("__DOGS_SVG__", DOGS_SVG.replace("`", "'"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _send(self, status, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        # Used by the page to detect a user-supplied photo.
        if self.path.startswith("/static/"):
            path = self._static_path()
            if path and os.path.isfile(path):
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
                self.end_headers()
                return
        self.send_response(404)
        self.end_headers()

    def _static_path(self):
        rel = self.path[len("/static/"):].split("?", 1)[0]
        if "/" in rel or ".." in rel or not rel:
            return None
        return os.path.join(STATIC_DIR, rel)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if self.path.startswith("/api/walk"):
            try:
                data = get_advice()
                self._send(200, json.dumps(data), "application/json")
            except Exception as e:
                self._send(
                    502,
                    json.dumps({"error": str(e)}),
                    "application/json",
                )
            return
        if self.path.startswith("/static/"):
            path = self._static_path()
            if path and os.path.isfile(path):
                with open(path, "rb") as f:
                    body = f.read()
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                self._send(200, body, ctype)
                return
            self._send(404, "not found", "text/plain")
            return
        self._send(404, "not found", "text/plain")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ip = lan_ip()
    print(f"Vegas & Odin Walk Advisor")
    print(f"  Local:   http://127.0.0.1:{PORT}")
    print(f"  Network: http://{ip}:{PORT}")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
