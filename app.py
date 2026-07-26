"""
Plane Radar - local ADS-B radar web app.

Shows nearby aircraft around a chosen ICAO airport code or lat/lon,
styled after ironicbadger's ESP32-Plane-Radar round display.

Data sources:
  - https://opendata.adsb.fi/          (live aircraft positions)
  - https://www.adsbdb.com/            (flight route / airline enrichment)
  - https://ourairports.com/data/      (ICAO airport -> lat/lon lookup)
  - https://open-meteo.com/            (current weather, no API key needed)

Run:
  pip install -r requirements.txt
  python app.py
  open http://localhost:8765
"""
import csv
import io
import json
import math
import os
import time
import threading
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
AIRPORTS_JSON = DATA_DIR / "airports.json"
AIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

ADSB_FI_BASE = "https://opendata.adsb.fi/api/v3"
# Fallback live-position source, used only if adsb.fi itself fails to respond.
# Same underlying data model (readsb/ADSBExchange-v2-shaped JSON, "ac" list),
# same 250nm point-query cap as adsb.fi - not a way around that limit, just
# a second, independent feeder network to fall back on if adsb.fi is down.
AIRPLANES_LIVE_BASE = "https://api.airplanes.live/v2"
ADSBDB_BASE = "https://api.adsbdb.com/v0"

AIRCRAFT_TTL = 2.5          # seconds - respects adsb.fi's public 1 req/s limit
MAX_DISPLAY_RANGE_NM = 3000  # radar zoom can go this far out for display purposes
MAX_QUERY_DIST_NM = 250      # adsb.fi's public dist endpoint hard-caps here
ROUTE_HIT_TTL = 6 * 3600    # cache known routes for 6h (mirrors the ESP32 project)
ROUTE_MISS_TTL = 10 * 60    # cache "unknown" results for 10 min
WEATHER_TTL = 15 * 60

app = Flask(__name__)

_aircraft_cache = {}
_route_cache = {}
_weather_cache = {}
_airports = {}
_airports_lock = threading.Lock()


def _http_get_json(url, timeout=8):
    req = Request(url, headers={"User-Agent": "plane-radar/1.0 (local, personal use)"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_airports():
    """Load ICAO -> {lat, lon, name, iata, municipality}, downloading + caching on first run."""
    global _airports
    with _airports_lock:
        if _airports:
            return _airports
        if AIRPORTS_JSON.exists():
            _airports = json.loads(AIRPORTS_JSON.read_text())
            return _airports

        print("First run: downloading airport database from OurAirports (one-time, ~10 MB)...")
        DATA_DIR.mkdir(exist_ok=True)
        req = Request(AIRPORTS_CSV_URL, headers={"User-Agent": "plane-radar/1.0"})
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        reader = csv.DictReader(io.StringIO(raw))
        airports = {}
        for row in reader:
            icao = (row.get("icao_code") or row.get("ident") or "").strip().upper()
            if len(icao) != 4 or not icao.isalnum():
                continue
            try:
                lat = float(row["latitude_deg"])
                lon = float(row["longitude_deg"])
            except (TypeError, ValueError, KeyError):
                continue
            airports[icao] = {
                "icao": icao,
                "iata": (row.get("iata_code") or "").strip().upper(),
                "name": (row.get("name") or "").strip(),
                "municipality": (row.get("municipality") or "").strip(),
                "lat": lat,
                "lon": lon,
            }

        AIRPORTS_JSON.write_text(json.dumps(airports))
        print(f"Cached {len(airports)} airports to {AIRPORTS_JSON}")
        _airports = airports
        return _airports


def get_airport(icao):
    return load_airports().get(icao.strip().upper())


def bearing_distance_nm(lat1, lon1, lat2, lon2):
    """Great-circle bearing (deg, 0=N/clockwise) and distance (nm) from point 1 to point 2."""
    R_NM = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    dist = 2 * R_NM * math.asin(min(1, math.sqrt(a)))
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    brng = (math.degrees(math.atan2(y, x)) + 360) % 360
    return brng, dist


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/airport/<icao>")
def api_airport(icao):
    apt = get_airport(icao)
    if not apt:
        return jsonify({"error": f"unknown ICAO code '{icao}'"}), 404
    return jsonify(apt)


@app.route("/api/aircraft")
def api_aircraft():
    icao = (request.args.get("icao") or "").strip().upper()
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    range_nm = request.args.get("range_nm", default=20, type=float)
    range_nm = max(1, min(range_nm, MAX_DISPLAY_RANGE_NM))
    # adsb.fi's public dist endpoint hard-caps at 250nm (empirically confirmed:
    # requests above 250 return empty). The radar can be zoomed out further than
    # that for display purposes, but the actual query never exceeds the cap.
    query_dist_nm = min(range_nm, MAX_QUERY_DIST_NM)

    center_name = None
    if icao:
        apt = get_airport(icao)
        if not apt:
            return jsonify({"error": f"unknown ICAO code '{icao}'"}), 404
        lat, lon, center_name = apt["lat"], apt["lon"], f"{apt['icao']}"
    if lat is None or lon is None:
        return jsonify({"error": "provide icao=XXXX or lat=&lon="}), 400

    # cached by query distance (not display range), since e.g. range_nm=800 and
    # range_nm=3000 both end up querying the same 250nm from adsb.fi
    cache_key = (round(lat, 4), round(lon, 4), round(query_dist_nm, 1))
    now = time.time()
    cached = _aircraft_cache.get(cache_key)
    if cached and now - cached[0] < AIRCRAFT_TTL:
        aircraft, data_source = cached[1], cached[2]
    else:
        data_source = "adsb.fi"
        try:
            data = _http_get_json(f"{ADSB_FI_BASE}/lat/{lat}/lon/{lon}/dist/{query_dist_nm}")
        except (HTTPError, URLError, TimeoutError) as primary_exc:
            # adsb.fi didn't respond at all (not just empty) - try the
            # independent airplanes.live feed before giving up.
            try:
                data = _http_get_json(f"{AIRPLANES_LIVE_BASE}/point/{lat}/{lon}/{query_dist_nm}")
                data_source = "airplanes.live"
            except (HTTPError, URLError, TimeoutError) as fallback_exc:
                return jsonify({
                    "error": f"adsb.fi request failed: {primary_exc}; "
                             f"airplanes.live fallback also failed: {fallback_exc}"
                }), 502

        aircraft = []
        for ac in data.get("ac", []):
            if ac.get("lat") is None or ac.get("lon") is None:
                continue
            brng, dist = bearing_distance_nm(lat, lon, ac["lat"], ac["lon"])
            alt_baro = ac.get("alt_baro")
            aircraft.append({
                "hex": ac.get("hex"),
                "flight": (ac.get("flight") or "").strip(),
                "reg": ac.get("r"),
                "type": ac.get("t"),
                "desc": ac.get("desc"),
                "alt_baro": alt_baro,
                "on_ground": alt_baro == "ground",
                "gs": ac.get("gs"),
                "track": ac.get("track"),
                "baro_rate": ac.get("baro_rate"),
                "squawk": ac.get("squawk"),
                "category": ac.get("category"),
                "lat": ac["lat"],
                "lon": ac["lon"],
                "bearing": round(brng, 1),
                "distance_nm": round(dist, 2),
            })
        aircraft.sort(key=lambda a: a["distance_nm"])
        _aircraft_cache[cache_key] = (now, aircraft, data_source)

    payload = {
        "center": {"lat": lat, "lon": lon, "name": center_name},
        "range_nm": range_nm,
        "query_dist_nm": query_dist_nm,
        "count": len(aircraft),
        "aircraft": aircraft,
        "fetched_at": now,
        "data_source": data_source,
    }
    return jsonify(payload)


@app.route("/api/route/<callsign>")
def api_route(callsign):
    callsign = callsign.strip().upper()
    if not callsign:
        return jsonify({"error": "callsign required"}), 400
    now = time.time()
    cached = _route_cache.get(callsign)
    if cached and now - cached[0] < cached[2]:
        return jsonify(cached[1])

    url = f"{ADSBDB_BASE}/callsign/{callsign}"
    try:
        data = _http_get_json(url)
        route = (data.get("response") or {}).get("flightroute")
        if route:
            origin = (route.get("origin") or {}).get("icao_code")
            dest = (route.get("destination") or {}).get("icao_code")
            result = {
                "callsign": callsign,
                "origin": origin,
                "destination": dest,
                "airline": (route.get("airline") or {}).get("name"),
            }
            _route_cache[callsign] = (now, result, ROUTE_HIT_TTL)
            return jsonify(result)
    except HTTPError as exc:
        if exc.code == 404:
            result = {"callsign": callsign, "origin": None, "destination": None, "airline": None}
            _route_cache[callsign] = (now, result, ROUTE_MISS_TTL)
            return jsonify(result)
    except (URLError, TimeoutError):
        pass

    result = {"callsign": callsign, "origin": None, "destination": None, "airline": None}
    _route_cache[callsign] = (now, result, ROUTE_MISS_TTL)
    return jsonify(result)


@app.route("/api/weather")
def api_weather():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    if lat is None or lon is None:
        return jsonify({"error": "lat & lon required"}), 400

    cache_key = (round(lat, 2), round(lon, 2))
    now = time.time()
    cached = _weather_cache.get(cache_key)
    if cached and now - cached[0] < WEATHER_TTL:
        return jsonify(cached[1])

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,weather_code"
        "&timezone=auto"
    )
    try:
        data = _http_get_json(url, timeout=6)
    except (HTTPError, URLError, TimeoutError):
        return jsonify({"error": "weather unavailable"}), 502

    cur = data.get("current", {})
    result = {
        "temperature_c": cur.get("temperature_2m"),
        "humidity": cur.get("relative_humidity_2m"),
        "weather_code": cur.get("weather_code"),
        "timezone": data.get("timezone"),
    }
    _weather_cache[cache_key] = (now, result)
    return jsonify(result)


if __name__ == "__main__":
    load_airports()
    port = int(os.environ.get("PORT", 8765))
    print(f"Plane Radar running at http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
