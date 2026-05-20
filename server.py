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
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Honor $PORT for cloud hosts (Render, Fly, Railway, Hugging Face all set it).
PORT = int(os.environ.get("PORT", "8080"))

CITIES = [
    {"name": "Bangkok", "lat": 13.7563, "lon": 100.5018, "tz": "Asia/Bangkok"},
    {"name": "Hua Hin", "lat": 12.5684, "lon": 99.9577,  "tz": "Asia/Bangkok"},
]

# Per-city cache of the last successful payload so a transient upstream
# failure for one city doesn't take the whole app down with a 502.
_LAST_OK = {}  # city name -> {"data": dict, "ts": float}
STALE_MAX_AGE_SEC = 60 * 60  # 1 hour

def weather_url(lat, lon, tz):
    tzq = urllib.parse.quote(tz, safe="")
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
        "precipitation,weather_code,wind_speed_10m,uv_index,is_day"
        f"&timezone={tzq}"
    )


def air_url(lat, lon, tz):
    tzq = urllib.parse.quote(tz, safe="")
    return (
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={lat}&longitude={lon}"
        "&current=pm2_5,pm10,us_aqi,ozone,nitrogen_dioxide"
        f"&timezone={tzq}"
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


def compute_advice(city):
    w_url = weather_url(city["lat"], city["lon"], city["tz"])
    a_url = air_url(city["lat"], city["lon"], city["tz"])
    w = fetch_json(w_url)["current"]
    # Air quality API is flakier; degrade gracefully if it fails.
    try:
        a = fetch_json(a_url)["current"]
        air_ok = True
    except Exception as e:
        print(f"  [{city['name']}] air-quality fetch failed: {e}")
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
        "city": city["name"],
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


def get_one_advice(city):
    """Fresh advice for one city, with stale-cache fallback on failure."""
    name = city["name"]
    try:
        data = compute_advice(city)
        _LAST_OK[name] = {"data": data, "ts": time.time()}
        return data
    except Exception as e:
        cached = _LAST_OK.get(name)
        age = time.time() - cached["ts"] if cached else None
        if cached and age is not None and age <= STALE_MAX_AGE_SEC:
            stale = dict(cached["data"])
            stale["stale"] = True
            stale["age_seconds"] = int(age)
            stale["notes"] = list(cached["data"].get("notes", [])) + [
                f"Live data unavailable ({type(e).__name__}); showing reading from "
                f"{int(age // 60)} min ago."
            ]
            return stale
        # No cache to fall back to — surface a per-city error rather than
        # taking down the whole response.
        return {"city": name, "error": str(e)}


def get_all_advice():
    """Fetch all cities in parallel; per-city failures don't block siblings."""
    with ThreadPoolExecutor(max_workers=max(2, len(CITIES))) as ex:
        return list(ex.map(get_one_advice, CITIES))


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

  .city-card {
    background: var(--card); border-radius: 18px; padding: 18px;
    margin-bottom: 14px; box-shadow: 0 2px 10px rgba(0,0,0,0.04);
  }
  .city-header {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; margin-bottom: 14px;
  }
  .city-header h2 {
    margin: 0; font-size: 20px; font-weight: 800; letter-spacing: -0.2px;
    text-transform: none; color: var(--text);
  }
  .stale-pill {
    background: var(--line); color: var(--muted);
    font-size: 11px; font-weight: 600; padding: 4px 9px;
    border-radius: 99px; white-space: nowrap;
  }
  .section-title {
    margin: 16px 0 10px; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted);
  }
  .city-card .msg { color: var(--text); opacity: 0.8; margin-top: 10px; }
  .city-card .verdict-badge { color: #fff; }

  .timer-slot { margin-top: 14px; }
  .timer-start {
    width: 100%; padding: 12px;
    background: transparent; color: var(--accent-dark);
    border: 2px dashed var(--accent); border-radius: 12px;
    font-size: 14px; font-weight: 700; cursor: pointer;
  }
  .timer-start:active { transform: scale(0.99); }
  @media (prefers-color-scheme: dark) {
    .timer-start { color: var(--accent); }
  }
  .timer-running {
    background: var(--bg); border-radius: 12px; padding: 12px;
    border: 2px solid var(--accent);
  }
  .timer-row {
    display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
    font-variant-numeric: tabular-nums;
  }
  .timer-elapsed { font-size: 22px; font-weight: 800; }
  .timer-max { font-size: 14px; color: var(--muted); }
  .timer-stop {
    margin-left: auto; background: var(--accent); color: #2a1900;
    border: none; padding: 6px 14px; border-radius: 8px;
    font-weight: 700; font-size: 13px; cursor: pointer;
  }
  .timer-bar {
    height: 6px; background: var(--line); border-radius: 99px; overflow: hidden;
  }
  .timer-bar > div {
    height: 100%; background: var(--accent); border-radius: 99px;
    transition: width 0.5s linear;
  }
  .timer-over { border-color: #c43a2e; background: rgba(196,58,46,0.12); }
  .timer-over .timer-elapsed { color: #c43a2e; }
  .timer-over .timer-bar > div { background: #c43a2e; }
  .timer-over-msg {
    margin-top: 8px; color: #c43a2e; font-weight: 700;
    font-size: 13px; text-align: center;
  }
  .timer-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.6);
    display: none; align-items: center; justify-content: center;
    z-index: 9999; padding: 20px;
  }
  .timer-overlay.show { display: flex; animation: fadein 0.2s ease; }
  @keyframes fadein { from { opacity: 0; } to { opacity: 1; } }
  .timer-overlay-card {
    background: var(--card); border-radius: 18px; padding: 24px;
    max-width: 360px; width: 100%; text-align: center;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  }
  .timer-overlay-title {
    font-size: 22px; font-weight: 800; color: #c43a2e;
    margin-bottom: 8px;
  }
  .timer-overlay-msg {
    font-size: 14px; line-height: 1.4; margin-bottom: 18px;
    color: var(--text);
  }
  .timer-overlay-card button {
    background: var(--accent); color: #2a1900;
    border: none; padding: 12px 28px; border-radius: 10px;
    font-size: 14px; font-weight: 700; cursor: pointer;
  }
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

function ring(score, color) {
  const R = 42, C = 2 * Math.PI * R;
  const off = C * (1 - score / 100);
  const fg = color || "#ffffff";
  return `
    <div class="ring-wrap">
      <svg width="96" height="96" viewBox="0 0 96 96">
        <circle cx="48" cy="48" r="${R}" stroke-width="8" fill="none"
                stroke="rgba(127,127,127,0.18)"/>
        <circle cx="48" cy="48" r="${R}" stroke-width="8" fill="none"
                stroke="${fg}" stroke-linecap="round"
                stroke-dasharray="${C}" stroke-dashoffset="${off}"
                style="transition: stroke-dashoffset 0.6s ease;"/>
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

function renderCity(d) {
  if (d.error) {
    return `<div class="city-card">
      <div class="city-header"><h2>${d.city || "Unknown"}</h2></div>
      <div class="err">Failed to load: ${d.error}</div>
    </div>`;
  }
  const f = d.factors, r = d.raw;
  const color = d.verdict_color || "#888";
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
  const stalePill = d.stale
    ? `<span class="stale-pill">${Math.floor((d.age_seconds || 0) / 60)} min old</span>`
    : "";

  return `
    <div class="city-card" data-city="${d.city}" data-max-min="${d.max_walk_minutes}">
      <div class="city-header">
        <h2>${d.city}</h2>
        ${stalePill}
      </div>
      <div class="verdict-row">
        ${ring(d.overall_score, color)}
        <div class="verdict-info">
          <div class="verdict-badge" style="background:${color}">${d.verdict}</div>
          <div class="max-walk">${d.max_walk_minutes} min<small>max walk</small></div>
        </div>
      </div>
      <div class="msg">${d.message}</div>

      <div class="timer-slot"></div>

      <div class="section-title">Factors</div>
      ${factor("&#x2600;&#xfe0f;", "Heat", f.heat)}
      ${factor("&#x1f343;", "Air",  f.air)}
      ${factor("&#x1f327;&#xfe0f;", "Rain", f.rain)}
      ${factor("&#x1f576;&#xfe0f;", "UV",   f.uv)}

      <div class="section-title">Conditions now</div>
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

      <div class="section-title">Notes</div>
      ${notesHtml}
    </div>`;
}

function render(data, photoUrl) {
  const cities = data.cities || [];
  const dogImg = photoUrl
    ? `<img class="dogs-img" src="${photoUrl}" alt="Vegas and Odin">`
    : `<div class="dogs-img" style="padding:8px">${DOGS_SVG_FALLBACK}</div>`;

  const topHero = `
    <div class="hero">
      <div class="hero-top">
        <div style="flex:1">
          <div class="hero-title">Vegas &amp; Odin</div>
          <div class="hero-sub">Walk Advisor &middot; Thailand</div>
        </div>
      </div>
      ${dogImg}
      <div class="names"><span>Vegas</span><span>Odin</span></div>
    </div>`;

  const updatedAt = new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  document.getElementById("root").innerHTML = `
    ${topHero}
    ${cities.map(renderCity).join("")}
    <button class="refresh-btn" onclick="load()">Refresh now</button>
    <div class="meta">Data: Open-Meteo &middot; updated ${updatedAt}<br>Goldens are double-coated &amp; heat-sensitive. Paw-test pavement (5-sec rule).</div>
  `;
  refreshTimerUI();
  if (getTimer()) scheduleTick();
}

// ---------- walk timer ----------

const TIMER_KEY = "walkTimer";
let tickInterval = null;
let notifiedForStartMs = null;
let chimeCtx = null;

function getTimer() {
  try { return JSON.parse(localStorage.getItem(TIMER_KEY)); } catch (e) { return null; }
}
function setTimer(t) {
  if (t) localStorage.setItem(TIMER_KEY, JSON.stringify(t));
  else localStorage.removeItem(TIMER_KEY);
}

async function startWalk(cityName, maxMinutes) {
  if ("Notification" in window && Notification.permission === "default") {
    try { await Notification.requestPermission(); } catch (e) {}
  }
  // Unlock audio context on this user gesture so the chime can play later
  // even if the tab loses focus (mobile browsers require a gesture first).
  try {
    chimeCtx = chimeCtx || new (window.AudioContext || window.webkitAudioContext)();
    if (chimeCtx.state === "suspended") await chimeCtx.resume();
  } catch (e) {}
  setTimer({ city: cityName, startMs: Date.now(), maxMinutes });
  notifiedForStartMs = null;
  scheduleTick();
  refreshTimerUI();
}

function stopWalk() {
  setTimer(null);
  notifiedForStartMs = null;
  if (tickInterval) { clearInterval(tickInterval); tickInterval = null; }
  refreshTimerUI();
}

function scheduleTick() {
  if (tickInterval) return;
  tickInterval = setInterval(refreshTimerUI, 1000);
}

function fmtMMSS(sec) {
  sec = Math.max(0, Math.floor(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function refreshTimerUI() {
  const t = getTimer();
  document.querySelectorAll(".city-card[data-city]").forEach(node => {
    const city = node.dataset.city;
    const maxFromCard = parseInt(node.dataset.maxMin, 10) || 0;
    const slot = node.querySelector(".timer-slot");
    if (!slot) return;

    if (t && t.city === city) {
      const elapsedSec = (Date.now() - t.startMs) / 1000;
      const maxSec = t.maxMinutes * 60;
      const pct = Math.min(100, (elapsedSec / Math.max(1, maxSec)) * 100);
      const over = elapsedSec >= maxSec;
      slot.innerHTML = `
        <div class="timer-running ${over ? "timer-over" : ""}">
          <div class="timer-row">
            <span class="timer-elapsed">${fmtMMSS(elapsedSec)}</span>
            <span class="timer-max">/ ${t.maxMinutes}:00</span>
            <button class="timer-stop" onclick="stopWalk()">Stop</button>
          </div>
          <div class="timer-bar"><div style="width:${pct}%"></div></div>
          ${over ? `<div class="timer-over-msg">Time's up — head back inside.</div>` : ""}
        </div>`;
      if (over && notifiedForStartMs !== t.startMs) {
        notifiedForStartMs = t.startMs;
        fireMaxWalkAlert(city);
      }
    } else {
      const disabled = (maxFromCard <= 0) ? "disabled" : "";
      const label = (maxFromCard <= 0)
        ? "Not safe to walk now"
        : `Start ${maxFromCard}-min walk`;
      slot.innerHTML = `<button class="timer-start" ${disabled}
        onclick="startWalk('${city.replace(/'/g, "\\'")}', ${maxFromCard})">${label}</button>`;
    }
  });

  if (!t && tickInterval) {
    clearInterval(tickInterval);
    tickInterval = null;
  }
}

function fireMaxWalkAlert(city) {
  showOverlay(city);
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      new Notification(`Walk time up - ${city}`, {
        body: "Vegas & Odin have hit the max safe walk duration. Head back inside.",
        tag: "walk-up",
      });
    } catch (e) {}
  }
  try { navigator.vibrate && navigator.vibrate([300, 150, 300, 150, 600]); } catch (e) {}
  playChime();
}

function showOverlay(city) {
  let overlay = document.getElementById("timer-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.id = "timer-overlay";
    overlay.className = "timer-overlay";
    document.body.appendChild(overlay);
  }
  overlay.innerHTML = `
    <div class="timer-overlay-card">
      <div class="timer-overlay-title">Time's up - ${city}</div>
      <div class="timer-overlay-msg">Vegas &amp; Odin have hit the max safe walk duration. Head back inside.</div>
      <button onclick="dismissOverlay()">Got it</button>
    </div>`;
  overlay.classList.add("show");
}
function dismissOverlay() {
  const overlay = document.getElementById("timer-overlay");
  if (overlay) overlay.classList.remove("show");
}

function playChime() {
  try {
    const ctx = chimeCtx || new (window.AudioContext || window.webkitAudioContext)();
    chimeCtx = ctx;
    const beep = (freq, when, dur) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "sine"; o.frequency.value = freq;
      o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0, ctx.currentTime + when);
      g.gain.linearRampToValueAtTime(0.25, ctx.currentTime + when + 0.02);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + when + dur);
      o.start(ctx.currentTime + when);
      o.stop(ctx.currentTime + when + dur + 0.05);
    };
    beep(880, 0.00, 0.45);
    beep(660, 0.55, 0.45);
    beep(880, 1.10, 0.55);
  } catch (e) {}
}

async function load() {
  const btn = document.querySelector(".refresh-btn");
  if (btn) {
    btn.disabled = true;
    btn.dataset.prev = btn.textContent;
    btn.textContent = "Refreshing…";
  }
  try {
    const [res, photoUrl] = await Promise.all([
      fetch("/api/walk?_=" + Date.now()),
      checkPhoto(),
    ]);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    render(data, photoUrl);
    // render() replaced the DOM, so the new button is fresh. Briefly pulse it
    // so the user sees the refresh completed.
    const fresh = document.querySelector(".refresh-btn");
    if (fresh) {
      fresh.classList.add("pulse");
      setTimeout(() => fresh.classList.remove("pulse"), 600);
    }
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
                cities = get_all_advice()
                self._send(200, json.dumps({"cities": cities}), "application/json")
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
