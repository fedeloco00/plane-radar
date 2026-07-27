"""
Plane Radar - local ADS-B radar web app.

Shows nearby aircraft around a chosen ICAO airport code or lat/lon,
styled after ironicbadger's ESP32-Plane-Radar round display.

Fully self-contained: this one file is all you need (plus
requirements.txt) to run it on any machine - the page markup, CSS, and
JS are embedded below as constants instead of living in templates/
and static/ folders, so there's nothing else to copy around.

Data sources:
  - https://opendata.adsb.fi/          (live aircraft positions)
  - https://api.airplanes.live/        (fallback live positions)
  - https://www.adsbdb.com/            (flight route / airline enrichment)
  - https://ourairports.com/data/      (ICAO airport -> lat/lon lookup)
  - https://open-meteo.com/            (current weather, no API key needed)

Run:
  pip install -r requirements.txt
  python app.py
  open http://localhost:8765

First run downloads and caches a small airport database to ./data/ -
that folder is regenerated automatically and doesn't need to be copied
between machines either.
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

from flask import Flask, Response, jsonify, request

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

app = Flask(__name__, static_folder=None)

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


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Plane Radar</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <div class="app">
    <header class="controls">
      <div class="control-group">
        <label for="icaoInput">Airport ICAO</label>
        <input id="icaoInput" type="text" maxlength="4" placeholder="KRDU" autocomplete="off" spellcheck="false">
      </div>
      <button id="goBtn">Go</button>
      <button id="geoBtn" title="Center on my current location">Use my location</button>
      <div class="control-group zoom-group">
        <label>Range</label>
        <button id="zoomOutBtn" class="zoom-btn" title="Zoom out">&minus;</button>
        <span id="rangeValue">25 nm</span>
        <button id="zoomInBtn" class="zoom-btn" title="Zoom in">&plus;</button>
        <span class="zoom-hint">scroll / pinch the radar to zoom</span>
      </div>
      <label class="toggle"><input id="routesToggle" type="checkbox" checked> routes</label>
      <label class="toggle"><input id="weatherToggle" type="checkbox" checked> weather</label>
      <div class="control-group">
        <label for="callsignFilter">Callsign</label>
        <input id="callsignFilter" class="callsign-input" type="text" placeholder="NJE, AF" autocomplete="off" spellcheck="false" title="Comma-separated callsign prefixes, e.g. NJE, AF">
      </div>
      <div class="control-group">
        <label for="registrationFilter">Registration</label>
        <input id="registrationFilter" class="callsign-input" type="text" placeholder="N, G-" autocomplete="off" spellcheck="false" title="Comma-separated registration prefixes, e.g. N, G-">
      </div>
      <div class="control-group alt-filter">
        <button id="altFilterBtn" type="button">Altitude &#9662;</button>
        <div id="altFilterMenu" class="alt-filter-menu hidden">
          <label><input type="checkbox" data-band="ground-5000" checked> Ground &ndash; 5,000 ft</label>
          <label><input type="checkbox" data-band="5000-10000" checked> 5,000 &ndash; 10,000 ft</label>
          <label><input type="checkbox" data-band="10000-20000" checked> 10,000 &ndash; 20,000 ft</label>
          <label><input type="checkbox" data-band="20000-30000" checked> 20,000 &ndash; 30,000 ft</label>
          <label><input type="checkbox" data-band="30000-40000" checked> 30,000 &ndash; 40,000 ft</label>
          <label><input type="checkbox" data-band="40000-50000" checked> 40,000 &ndash; 50,000 ft</label>
          <div class="alt-filter-sep"></div>
          <label><input type="checkbox" id="hideGroundTraffic"> Remove ground traffic</label>
          <div class="alt-filter-actions">
            <button id="altFilterAll" type="button">All</button>
            <button id="altFilterNone" type="button">None</button>
          </div>
        </div>
      </div>
      <span id="statusMsg" class="status"></span>
    </header>

    <main class="layout">
      <div class="radar-wrap">
        <div class="view-toggle">
          <button id="viewRadarBtn" class="view-btn active" type="button">Radar</button>
          <button id="viewMapBtn" class="view-btn" type="button">Map</button>
        </div>
        <div class="bezel" id="radarBezel">
          <canvas id="radar" width="640" height="640"></canvas>
        </div>
        <div class="map-bezel hidden" id="mapBezel">
          <div id="mapView"></div>
        </div>
        <div class="footer">
          <div id="footerLoc" class="footer-loc">--</div>
          <div id="footerWeather" class="footer-weather">&nbsp;</div>
          <div id="footerTime" class="footer-time">--:-- -- ---</div>
        </div>
      </div>

      <aside class="sidebar">
        <div id="detailsPanel" class="details-panel hidden">
          <div class="details-header">
            <h2 id="detailsTitle">--</h2>
            <button id="detailsClose" class="close-btn" title="Close">&times;</button>
          </div>
          <div class="details-body">
            <div class="details-row"><span class="details-label">Type</span><span id="detailsType">--</span></div>
            <div class="details-row"><span class="details-label">Registration</span><span id="detailsReg">--</span></div>
            <div class="details-row"><span class="details-label">Route</span><span id="detailsRoute">--</span></div>
            <div class="details-row"><span class="details-label">Altitude</span><span id="detailsAlt">--</span></div>
            <div class="details-row"><span class="details-label">Ground speed</span><span id="detailsGs">--</span></div>
            <div class="details-row"><span class="details-label">Heading</span><span id="detailsTrack">--</span></div>
            <div class="details-row"><span class="details-label">Distance / Brg</span><span id="detailsDist">--</span></div>
            <div class="details-row"><span class="details-label">Squawk</span><span id="detailsSquawk">--</span></div>
          </div>
        </div>

        <h2>Aircraft <span id="aircraftCount">(0)</span></h2>
        <table id="aircraftTable">
          <thead>
            <tr><th>Flight</th><th>Type</th><th>Alt</th><th>Dist</th><th>Brg</th></tr>
          </thead>
          <tbody id="aircraftBody"></tbody>
        </table>
      </aside>
    </main>
  </div>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
  <script src="/static/radar.js"></script>
</body>
</html>
"""

STYLE_CSS = r""":root {
  --bg: #05070a;
  --panel: #0c1117;
  --green: #33d17a;
  --green-dim: #1c6e40;
  --cyan: #4fd7ff;
  --yellow: #ffd54a;
  --magenta: #ff3ec8;
  --red: #ff5533;
  --text: #e8eef2;
  --dim-text: #7d8b95;
  --border: #1c242c;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: "SF Mono", "Cascadia Code", Consolas, "Courier New", monospace;
  height: 100%;
}

.app {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.controls {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 12px 20px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}

.control-group {
  display: flex;
  align-items: center;
  gap: 6px;
}

.control-group label {
  font-size: 12px;
  color: var(--dim-text);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

input[type="text"], select {
  background: #10161d;
  border: 1px solid var(--border);
  color: var(--text);
  padding: 6px 10px;
  border-radius: 4px;
  font-family: inherit;
  font-size: 14px;
}

input#icaoInput {
  width: 90px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
}

input.callsign-input {
  width: 110px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

button {
  background: var(--green-dim);
  color: #eafff2;
  border: 1px solid var(--green);
  padding: 7px 14px;
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
  font-size: 13px;
}

button:hover { background: var(--green); color: #04170c; }

.alt-filter {
  position: relative;
}

.alt-filter-menu {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  /* Needs to beat Leaflet's own internal z-indexes (panes go up to ~700,
     its zoom/attribution controls sit at 1000) so the dropdown isn't
     drawn underneath the map view when that's the active view. */
  z-index: 3000;
  background: #0c1117;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 190px;
  box-shadow: 0 8px 20px rgba(0,0,0,0.5);
}

.alt-filter-menu.hidden { display: none; }

.alt-filter-menu label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text);
  cursor: pointer;
}

.alt-filter-sep {
  height: 1px;
  background: var(--border);
  margin: 2px 0;
}

.alt-filter-actions {
  display: flex;
  gap: 8px;
  margin-top: 4px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}

.alt-filter-actions button {
  flex: 1;
  padding: 4px 0;
  font-size: 12px;
}

.zoom-group { gap: 8px; }

.zoom-btn {
  padding: 4px 11px;
  font-size: 15px;
  line-height: 1;
}

#rangeValue {
  min-width: 52px;
  text-align: center;
  font-size: 13px;
  color: var(--text);
}

.zoom-hint {
  font-size: 11px;
  color: var(--dim-text);
  font-style: italic;
}

.toggle {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 13px;
  color: var(--dim-text);
}

.status {
  margin-left: auto;
  font-size: 12px;
  color: var(--dim-text);
}
.status.error { color: var(--red); }

.layout {
  flex: 1;
  display: flex;
  gap: 24px;
  padding: 24px;
  flex-wrap: wrap;
  justify-content: center;
}

.radar-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

.view-toggle {
  display: flex;
  gap: 6px;
}

.view-btn {
  background: transparent;
  color: var(--dim-text);
  border: 1px solid var(--border);
}

.view-btn.active {
  background: var(--green-dim);
  color: #eafff2;
  border-color: var(--green);
}

.map-bezel {
  width: 660px;
  height: 660px;
  max-width: 90vw;
  max-height: 90vw;
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid var(--border);
  box-shadow: 0 0 30px rgba(0,0,0,0.6);
}

.map-bezel.hidden, .bezel.hidden { display: none; }

#mapView {
  width: 100%;
  height: 100%;
  background: #04080b;
}

/* Leaflet dark-theme tweaks to match the app */
.leaflet-container {
  background: #04080b;
  font-family: inherit;
}

.leaflet-popup-content-wrapper, .leaflet-popup-tip {
  background: #0c1117;
  color: var(--text);
}

.leaflet-control-attribution {
  background: rgba(12, 17, 23, 0.75) !important;
  color: var(--dim-text) !important;
}
.leaflet-control-attribution a { color: var(--cyan) !important; }

.plane-tooltip {
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
  padding: 0 !important;
  font-family: inherit;
  font-size: 15px;
  line-height: 1.35;
  white-space: nowrap;
}
.plane-tooltip::before { display: none; }
.plane-tip-call { color: var(--text); }
.plane-tip-type { color: var(--cyan); }
.plane-tip-alt { color: var(--alt, #ffb648); }
.plane-tip-selected .plane-tip-call { color: var(--green); }

.home-tooltip {
  background: transparent !important;
  border: none !important;
  box-shadow: none !important;
  color: var(--text) !important;
  font-weight: bold;
  font-family: inherit;
}
.home-tooltip::before { display: none; }

.bezel {
  width: 660px;
  height: 660px;
  max-width: 90vw;
  max-height: 90vw;
  border-radius: 50%;
  background: radial-gradient(circle at 35% 30%, #2a3138, #05070a 70%);
  padding: 10px;
  box-shadow: inset 0 0 20px #000, 0 0 30px rgba(0,0,0,0.6);
  display: flex;
  align-items: center;
  justify-content: center;
}

canvas#radar {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  background: #04080b;
}

.footer {
  text-align: center;
  line-height: 1.5;
}

.footer-loc {
  font-size: 20px;
  letter-spacing: 0.08em;
  color: var(--text);
}

.footer-weather {
  font-size: 13px;
  color: var(--cyan);
}

.footer-time {
  font-size: 13px;
  color: var(--dim-text);
}

.sidebar {
  width: 360px;
  max-width: 90vw;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  height: fit-content;
}

.sidebar h2 {
  margin: 0 0 10px;
  font-size: 14px;
  color: var(--dim-text);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 500;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

thead th {
  text-align: left;
  color: var(--dim-text);
  font-weight: 500;
  padding: 4px 6px;
  border-bottom: 1px solid var(--border);
}

tbody td {
  padding: 5px 6px;
  border-bottom: 1px solid #12181f;
  color: var(--text);
}

tbody tr:hover { background: #10161d; cursor: pointer; }
tbody tr.selected { background: #142219; box-shadow: inset 2px 0 0 var(--green); }

.flight-cell { color: var(--yellow); }
.type-cell { color: var(--cyan); }

canvas#radar { cursor: pointer; }

.details-panel {
  border: 1px solid var(--green-dim);
  border-radius: 6px;
  background: #0a1712;
  margin-bottom: 16px;
  overflow: hidden;
}

.details-panel.hidden { display: none; }

.details-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #0e1f16;
  border-bottom: 1px solid var(--green-dim);
}

.details-header h2 {
  margin: 0;
  font-size: 15px;
  color: var(--yellow);
  text-transform: none;
  letter-spacing: 0.03em;
}

.close-btn {
  background: transparent;
  border: none;
  color: var(--dim-text);
  font-size: 18px;
  line-height: 1;
  padding: 0 4px;
}
.close-btn:hover { color: var(--red); background: transparent; }

.details-body { padding: 10px 12px; }

.details-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 5px 0;
  border-bottom: 1px solid #12211a;
  font-size: 13px;
}
.details-row:last-child { border-bottom: none; }

.details-label {
  color: var(--dim-text);
  text-transform: uppercase;
  font-size: 11px;
  letter-spacing: 0.04em;
  padding-top: 2px;
}

.details-row span:last-child {
  text-align: right;
  color: var(--text);
}
"""

RADAR_JS = r"""(() => {
  "use strict";

  const COLORS = {
    bg: "#04080b",
    ring: "#1c6e40",
    ringDim: "#123a24",
    crosshair: "#123a24",
    text: "#e8eef2",
    dim: "#6f8a7c",
    heading: "#ff5533",
    headingGround: "#5a6570",
    vector: "#ff3ec8",
    callsign: "#e8eef2",
    type: "#4fd7ff",
    alt: "#ffb648",
    home: "#e8eef2",
    selected: "#33d17a",
    limitRing: "#ffb648",
  };

  const AIRCRAFT_POLL_MS = 4000;
  const WEATHER_POLL_MS = 15 * 60 * 1000;
  const ROUTE_QUEUE_DELAY_MS = 350;
  const HIT_RADIUS_PX = 22; // logical canvas units
  const MIN_RANGE_NM = 1;
  // Display/zoom limit. Note: adsb.fi's public dist endpoint hard-caps actual
  // queries at 250nm regardless of zoom - the backend clamps for us and tells
  // us via query_dist_nm in the response, so we can draw exactly where that
  // boundary falls whenever it's smaller than the current display range.
  const MAX_RANGE_NM = 3000;
  const DEFAULT_RANGE_NM = 25;
  const RANGE_REFETCH_DEBOUNCE_MS = 450;
  const NM_TO_M = 1852; // 1 nautical mile in meters, for Leaflet circle radii

  // Free, no-signup dark basemap built on OpenStreetMap data - chosen for
  // the dark UI theme. Requires attributing both OSM and CARTO (below).
  const MAP_TILE_URL = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png";
  const MAP_ATTRIBUTION =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors ' +
    '&copy; <a href="https://carto.com/attributions">CARTO</a>';

  const ALT_BANDS = [
    { id: "ground-5000", min: 0, max: 5000, label: "Ground – 5,000 ft" },
    { id: "5000-10000", min: 5000, max: 10000, label: "5,000 – 10,000 ft" },
    { id: "10000-20000", min: 10000, max: 20000, label: "10,000 – 20,000 ft" },
    { id: "20000-30000", min: 20000, max: 30000, label: "20,000 – 30,000 ft" },
    { id: "30000-40000", min: 30000, max: 40000, label: "30,000 – 40,000 ft" },
    { id: "40000-50000", min: 40000, max: 50000, label: "40,000 – 50,000 ft" },
  ];

  const els = {
    icaoInput: document.getElementById("icaoInput"),
    goBtn: document.getElementById("goBtn"),
    geoBtn: document.getElementById("geoBtn"),
    routesToggle: document.getElementById("routesToggle"),
    weatherToggle: document.getElementById("weatherToggle"),
    statusMsg: document.getElementById("statusMsg"),
    canvas: document.getElementById("radar"),
    footerLoc: document.getElementById("footerLoc"),
    footerWeather: document.getElementById("footerWeather"),
    footerTime: document.getElementById("footerTime"),
    aircraftCount: document.getElementById("aircraftCount"),
    aircraftBody: document.getElementById("aircraftBody"),
    callsignFilter: document.getElementById("callsignFilter"),
    registrationFilter: document.getElementById("registrationFilter"),
    zoomInBtn: document.getElementById("zoomInBtn"),
    zoomOutBtn: document.getElementById("zoomOutBtn"),
    rangeValue: document.getElementById("rangeValue"),
    altFilterBtn: document.getElementById("altFilterBtn"),
    altFilterMenu: document.getElementById("altFilterMenu"),
    altFilterAll: document.getElementById("altFilterAll"),
    altFilterNone: document.getElementById("altFilterNone"),
    hideGroundTraffic: document.getElementById("hideGroundTraffic"),
    detailsPanel: document.getElementById("detailsPanel"),
    detailsClose: document.getElementById("detailsClose"),
    detailsTitle: document.getElementById("detailsTitle"),
    detailsType: document.getElementById("detailsType"),
    detailsReg: document.getElementById("detailsReg"),
    detailsRoute: document.getElementById("detailsRoute"),
    detailsAlt: document.getElementById("detailsAlt"),
    detailsGs: document.getElementById("detailsGs"),
    detailsTrack: document.getElementById("detailsTrack"),
    detailsDist: document.getElementById("detailsDist"),
    detailsSquawk: document.getElementById("detailsSquawk"),
    viewRadarBtn: document.getElementById("viewRadarBtn"),
    viewMapBtn: document.getElementById("viewMapBtn"),
    radarBezel: document.getElementById("radarBezel"),
    mapBezel: document.getElementById("mapBezel"),
  };

  const ctx = els.canvas.getContext("2d");
  const CANVAS_SIZE = els.canvas.width; // logical drawing units (square)

  const state = {
    mode: null,        // "icao" | "latlon"
    icao: null,
    lat: null,
    lon: null,
    rangeNm: DEFAULT_RANGE_NM,
    locationLabel: "--",
    aircraftTimer: null,
    weatherTimer: null,
    routeCache: new Map(),      // callsign -> {origin, destination, airline, ts}
    routeQueue: [],
    routeQueueBusy: false,
    airportInfoCache: new Map(), // icao -> {name, municipality, iata} | null
    lastMarkers: [],             // [{x, y, ac}] from most recent draw, for click hit-testing
    selectedHex: null,
    lastAircraftByHex: new Map(),
    lastQueryDistNm: null,  // adsb.fi's actual query radius from the last successful fetch
    viewMode: "radar",      // "radar" | "map"
    map: null,              // Leaflet map instance, created lazily on first switch to map view
    aircraftLayer: null,    // Leaflet layerGroup holding aircraft markers
    mapMarkers: new Map(),  // hex -> L.marker, kept across refreshes for smooth updates
    mapQueryCircle: null,   // L.circle showing the query radius
    mapLimitCircle: null,   // L.circle showing the adsb.fi 250nm data-limit boundary
    homeMarker: null,       // L.marker at the chosen center point
    activeAltBands: new Set(ALT_BANDS.map((b) => b.id)),
    callsignPrefixes: [],   // [] = no filter, show everything
    registrationPrefixes: [],
    hideGroundTraffic: false,  // overrides the ground-5000 band: force-hide on_ground aircraft
  };

  function altitudeVisible(ac) {
    if (ac.on_ground) {
      if (state.hideGroundTraffic) return false;
      return state.activeAltBands.has("ground-5000");
    }
    if (ac.alt_baro == null || typeof ac.alt_baro !== "number") return true; // unknown altitude - never hide
    for (const band of ALT_BANDS) {
      const upperInclusive = band.id === ALT_BANDS[ALT_BANDS.length - 1].id;
      if (ac.alt_baro >= band.min && (upperInclusive ? ac.alt_baro <= band.max : ac.alt_baro < band.max)) {
        return state.activeAltBands.has(band.id);
      }
    }
    return true; // above the highest band - not filtered
  }

  function callsignVisible(ac) {
    if (state.callsignPrefixes.length === 0) return true;
    const cs = (ac.flight || "").trim().toUpperCase();
    if (!cs) return false; // no callsign broadcast - can't match a prefix filter
    return state.callsignPrefixes.some((p) => cs.startsWith(p));
  }

  function registrationVisible(ac) {
    if (state.registrationPrefixes.length === 0) return true;
    const reg = (ac.reg || "").trim().toUpperCase();
    if (!reg) return false; // no registration broadcast - can't match a prefix filter
    return state.registrationPrefixes.some((p) => reg.startsWith(p));
  }

  function isAircraftVisible(ac) {
    return altitudeVisible(ac) && callsignVisible(ac) && registrationVisible(ac);
  }

  function parsePrefixListInput(value) {
    return (value || "")
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter((s) => s.length > 0);
  }

  function applyFiltersAndRender() {
    const all = Array.from(state.lastAircraftByHex.values());
    const filtered = all.filter(isAircraftVisible);
    renderCurrentView(filtered, state.rangeNm, state.lastQueryDistNm);
    updateSidebar(filtered, all.length);
  }

  function setStatus(msg, isError) {
    els.statusMsg.textContent = msg || "";
    els.statusMsg.classList.toggle("error", !!isError);
  }

  function saveLastLocation() {
    const data = {
      mode: state.mode, icao: state.icao, lat: state.lat, lon: state.lon,
      rangeNm: state.rangeNm, altBands: Array.from(state.activeAltBands),
      callsignPrefixes: state.callsignPrefixes, registrationPrefixes: state.registrationPrefixes,
      viewMode: state.viewMode, hideGroundTraffic: state.hideGroundTraffic,
    };
    try { localStorage.setItem("plane-radar-last", JSON.stringify(data)); } catch (e) {}
  }

  function loadLastLocation() {
    try {
      const raw = localStorage.getItem("plane-radar-last");
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  // ---------- location setup ----------

  async function goToIcao(icao) {
    icao = (icao || "").trim().toUpperCase();
    if (icao.length !== 4) {
      setStatus("Enter a 4-letter ICAO code, e.g. KRDU", true);
      return;
    }
    setStatus("Looking up " + icao + "...");
    try {
      const resp = await fetch(`/api/airport/${icao}`);
      if (!resp.ok) throw new Error((await resp.json()).error || "not found");
      const apt = await resp.json();
      state.mode = "icao";
      state.icao = icao;
      state.lat = apt.lat;
      state.lon = apt.lon;
      state.locationLabel = apt.icao;
      setStatus("");
      restartPolling();
      saveLastLocation();
    } catch (err) {
      setStatus("Airport lookup failed: " + err.message, true);
    }
  }

  function goToLatLon(lat, lon, label) {
    state.mode = "latlon";
    state.icao = null;
    state.lat = lat;
    state.lon = lon;
    state.locationLabel = label || `${lat.toFixed(2)}, ${lon.toFixed(2)}`;
    setStatus("");
    restartPolling();
    saveLastLocation();
  }

  function useMyLocation() {
    if (!navigator.geolocation) {
      setStatus("Geolocation not supported by this browser", true);
      return;
    }
    setStatus("Requesting location...");
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        goToLatLon(pos.coords.latitude, pos.coords.longitude, "MY LOCATION");
        els.icaoInput.value = "";
      },
      (err) => setStatus("Location error: " + err.message, true),
      { enableHighAccuracy: false, timeout: 10000 }
    );
  }

  // ---------- polling ----------

  function restartPolling() {
    if (state.aircraftTimer) clearInterval(state.aircraftTimer);
    if (state.weatherTimer) clearInterval(state.weatherTimer);
    closeDetails();
    recenterMap(9); // no-op if the map hasn't been created yet - initMap() centers correctly on its own
    refreshAircraft();
    refreshWeather();
    state.aircraftTimer = setInterval(refreshAircraft, AIRCRAFT_POLL_MS);
    state.weatherTimer = setInterval(refreshWeather, WEATHER_POLL_MS);
  }

  async function refreshAircraft() {
    if (state.lat == null || state.lon == null) return;
    const params = new URLSearchParams();
    if (state.mode === "icao") params.set("icao", state.icao);
    else { params.set("lat", state.lat); params.set("lon", state.lon); }
    params.set("range_nm", state.rangeNm);

    try {
      const resp = await fetch("/api/aircraft?" + params.toString());
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "request failed");
      const sourceNote = data.data_source && data.data_source !== "adsb.fi" ? ` (via ${data.data_source} fallback)` : "";
      const limitNote = data.query_dist_nm < data.range_nm ? ` · data limited to ${data.query_dist_nm}nm` : "";
      setStatus(`${data.count} aircraft · updated ${new Date().toLocaleTimeString()}${sourceNote}${limitNote}`);

      state.lastAircraftByHex = new Map(data.aircraft.map((ac) => [ac.hex, ac]));
      state.lastQueryDistNm = data.query_dist_nm;

      const filtered = data.aircraft.filter(isAircraftVisible);
      renderCurrentView(filtered, data.range_nm, data.query_dist_nm);
      updateSidebar(filtered, data.aircraft.length);
      if (els.routesToggle.checked) queueRouteLookups(filtered);
      if (state.selectedHex) refreshDetailsLiveFields();
    } catch (err) {
      setStatus("Aircraft fetch failed: " + err.message, true);
    }
  }

  async function refreshWeather() {
    if (!els.weatherToggle.checked || state.lat == null) {
      els.footerWeather.textContent = " ";
      return;
    }
    try {
      const resp = await fetch(`/api/weather?lat=${state.lat}&lon=${state.lon}`);
      const data = await resp.json();
      if (!resp.ok) return;
      els.footerWeather.textContent = formatWeather(data);
    } catch (e) {
      // silently ignore - weather is decorative
    }
  }

  function weatherCondLabel(code) {
    if (code === 0) return "CLR";
    if (code <= 2) return "FEW";
    if (code === 3) return "OVC";
    if (code === 45 || code === 48) return "FOG";
    if (code >= 51 && code <= 67) return "RAIN";
    if (code >= 71 && code <= 77) return "SNOW";
    if (code >= 80 && code <= 82) return "SHWR";
    if (code >= 95) return "TSTM";
    return "---";
  }

  function formatWeather(w) {
    if (w.temperature_c == null) return " ";
    const cond = weatherCondLabel(w.weather_code);
    const t = Math.round(w.temperature_c);
    const h = Math.round(w.humidity);
    return `${cond} ${t}°C RH${h}%`;
  }

  // ---------- route enrichment (throttled, background) ----------

  function queueRouteLookups(aircraftList) {
    const now = Date.now();
    for (const ac of aircraftList) {
      const cs = ac.flight;
      if (!cs) continue;
      const cached = state.routeCache.get(cs);
      if (cached && now - cached.ts < 6 * 3600 * 1000) continue;
      if (state.routeQueue.includes(cs)) continue;
      state.routeQueue.push(cs);
    }
    processRouteQueue();
  }

  function processRouteQueue() {
    if (state.routeQueueBusy || state.routeQueue.length === 0) return;
    state.routeQueueBusy = true;
    const cs = state.routeQueue.shift();
    fetchRoute(cs).finally(() => {
      state.routeQueueBusy = false;
      setTimeout(processRouteQueue, ROUTE_QUEUE_DELAY_MS);
    });
  }

  function fetchRoute(callsign) {
    return fetch(`/api/route/${encodeURIComponent(callsign)}`)
      .then((r) => r.json())
      .then(async (data) => {
        // prefer the 3-letter IATA code for the compact radar tag; fall back to
        // the full 4-letter ICAO code (never truncate it - ICAO codes aren't
        // "IATA + one extra letter" outside of the continental US)
        let originShort = data.origin || null;
        let destShort = data.destination || null;
        if (data.origin) {
          const info = await getAirportInfo(data.origin);
          if (info && info.iata) originShort = info.iata;
        }
        if (data.destination) {
          const info = await getAirportInfo(data.destination);
          if (info && info.iata) destShort = info.iata;
        }
        const entry = {
          origin: data.origin, destination: data.destination,
          originShort, destShort, airline: data.airline, ts: Date.now(),
        };
        state.routeCache.set(callsign, entry);
        return entry;
      })
      .catch(() => null);
  }

  function getAirportInfo(icao) {
    if (!icao) return Promise.resolve(null);
    if (state.airportInfoCache.has(icao)) return Promise.resolve(state.airportInfoCache.get(icao));
    return fetch(`/api/airport/${icao}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((info) => {
        state.airportInfoCache.set(icao, info);
        return info;
      })
      .catch(() => null);
  }

  function routeTag(ac) {
    const cached = state.routeCache.get(ac.flight);
    if (cached && cached.originShort && cached.destShort) {
      return `${cached.originShort}-${cached.destShort}`;
    }
    return ac.flight || ac.reg || ac.hex || "?????";
  }

  // ---------- sidebar table ----------

  function updateSidebar(aircraftList, totalCount) {
    els.aircraftCount.textContent = (totalCount != null && totalCount !== aircraftList.length)
      ? `(${aircraftList.length} of ${totalCount})`
      : `(${aircraftList.length})`;
    els.aircraftBody.innerHTML = "";
    for (const ac of aircraftList) {
      const tr = document.createElement("tr");
      tr.dataset.hex = ac.hex;
      if (ac.hex === state.selectedHex) tr.classList.add("selected");
      const alt = ac.on_ground ? "GND" : (ac.alt_baro != null ? `${ac.alt_baro} ft` : "--");
      tr.innerHTML = `
        <td class="flight-cell">${ac.flight || ac.reg || ac.hex}</td>
        <td class="type-cell">${ac.type || "--"}</td>
        <td>${alt}</td>
        <td>${ac.distance_nm.toFixed(1)} nm</td>
        <td>${Math.round(ac.bearing)}°</td>
      `;
      tr.addEventListener("click", () => selectAircraft(ac));
      els.aircraftBody.appendChild(tr);
    }
  }

  // ---------- radar drawing ----------

  function drawRadar(aircraftList, rangeNm, queryDistNm) {
    const size = CANVAS_SIZE;
    const cx = size / 2, cy = size / 2;
    const R = size / 2 - size * 0.08;

    state.lastMarkers = [];

    ctx.clearRect(0, 0, size, size);
    ctx.fillStyle = COLORS.bg;
    ctx.fillRect(0, 0, size, size);

    // rings
    ctx.strokeStyle = COLORS.ring;
    ctx.lineWidth = 1.5;
    for (let i = 1; i <= 4; i++) {
      ctx.beginPath();
      ctx.arc(cx, cy, (R * i) / 4, 0, Math.PI * 2);
      ctx.globalAlpha = 0.55;
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // crosshair
    ctx.strokeStyle = COLORS.crosshair;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
    ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
    ctx.stroke();

    // center dot
    ctx.fillStyle = COLORS.text;
    ctx.beginPath();
    ctx.arc(cx, cy, 3, 0, Math.PI * 2);
    ctx.fill();

    // compass labels
    ctx.fillStyle = COLORS.text;
    ctx.font = `bold ${Math.round(size * 0.032)}px monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("N", cx, cy - R - size * 0.03);
    ctx.fillText("S", cx, cy + R + size * 0.03);
    ctx.textAlign = "left";
    ctx.fillText("E", cx + R + size * 0.015, cy);
    ctx.textAlign = "right";
    ctx.fillText("W", cx - R - size * 0.015, cy);

    // range label on the 3/4 ring, east side
    ctx.fillStyle = COLORS.dim;
    ctx.font = `${Math.round(size * 0.024)}px monospace`;
    ctx.textAlign = "left";
    ctx.fillText(`${rangeNm}nm`, cx + (R * 3) / 4 + 4, cy - 4);

    // adsb.fi data-limit boundary: when the display range is zoomed out
    // further than adsb.fi actually queried, draw exactly where real data
    // stops so an empty outer area doesn't look like a bug.
    if (queryDistNm != null && queryDistNm < rangeNm) {
      const limitR = (queryDistNm / rangeNm) * R;
      ctx.save();
      ctx.strokeStyle = COLORS.limitRing;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 5]);
      ctx.globalAlpha = 0.85;
      ctx.beginPath();
      ctx.arc(cx, cy, limitR, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();

      ctx.fillStyle = COLORS.limitRing;
      ctx.font = `${Math.round(size * 0.022)}px monospace`;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      const labelAngle = -Math.PI / 4; // northeast spoke, away from the east range label
      ctx.fillText(
        `data limit: ${queryDistNm}nm`,
        cx + limitR * Math.sin(labelAngle) + 6,
        cy - limitR * Math.cos(labelAngle)
      );
    }

    // home marker label near center
    ctx.fillStyle = COLORS.home;
    ctx.font = `bold ${Math.round(size * 0.026)}px monospace`;
    ctx.textAlign = "center";
    ctx.fillText(state.locationLabel, cx, cy + size * 0.06);

    // aircraft
    for (const ac of aircraftList) {
      drawAircraft(ac, cx, cy, R, rangeNm, size);
    }
  }

  function drawAircraft(ac, cx, cy, R, rangeNm, size) {
    const bearingRad = (ac.bearing * Math.PI) / 180;
    const clamped = Math.min(ac.distance_nm, rangeNm);
    const rPx = (clamped / rangeNm) * R;
    const x = cx + rPx * Math.sin(bearingRad);
    const y = cy - rPx * Math.cos(bearingRad);

    state.lastMarkers.push({ x, y, ac });

    const isSelected = ac.hex === state.selectedHex;

    if (isSelected) {
      ctx.strokeStyle = COLORS.selected;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(x, y, size * 0.024, 0, Math.PI * 2);
      ctx.stroke();
    }

    if (ac.distance_nm > rangeNm) {
      // beyond outer ring: direction cue only
      ctx.fillStyle = COLORS.heading;
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
      return;
    }

    const trackRad = ((ac.track || 0) * Math.PI) / 180;
    const color = ac.on_ground ? COLORS.headingGround : COLORS.heading;

    // speed vector (magenta), length scaled by groundspeed
    if (!ac.on_ground && ac.gs) {
      const vecLen = Math.min(size * 0.05, Math.max(size * 0.015, (ac.gs / 500) * size * 0.05));
      ctx.strokeStyle = COLORS.vector;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + vecLen * Math.sin(trackRad), y - vecLen * Math.cos(trackRad));
      ctx.stroke();
    }

    // heading triangle
    const s = size * 0.014;
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(trackRad);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(0, -s * 1.6);
    ctx.lineTo(s, s);
    ctx.lineTo(-s, s);
    ctx.closePath();
    ctx.fill();
    ctx.restore();

    // label tag: route/callsign, type, altitude
    const west = x < cx;
    ctx.textAlign = west ? "left" : "right";
    const tx = x + (west ? size * 0.022 : -size * 0.022);
    let ty = y - size * 0.024;
    const lineH = size * 0.034;
    const fontSize = Math.round(size * 0.029);

    ctx.font = `${fontSize}px monospace`;
    ctx.fillStyle = isSelected ? COLORS.selected : COLORS.callsign;
    ctx.fillText(routeTag(ac), tx, ty);
    ty += lineH;

    if (ac.type) {
      ctx.fillStyle = COLORS.type;
      ctx.fillText(ac.type, tx, ty);
      ty += lineH;
    }

    ctx.fillStyle = COLORS.alt;
    const altText = ac.on_ground ? "GND" : (ac.alt_baro != null ? `${ac.alt_baro} ft` : "");
    if (altText) ctx.fillText(altText, tx, ty);
  }

  // ---------- map view (Leaflet + OpenStreetMap-derived tiles) ----------
  //
  // Fixed-radius model: the map is centered on the chosen location with the
  // query-radius circle drawn on it. You can freely pan/zoom the map itself
  // to look around, but the underlying data stays tied to that circle until
  // you re-center (same data as the radar view - just a different renderer).

  function initMap() {
    if (state.map) return state.map;
    const map = L.map(els.mapBezel.querySelector("#mapView"), {
      zoomControl: true,
      attributionControl: true,
    }).setView([state.lat || 0, state.lon || 0], 8);

    L.tileLayer(MAP_TILE_URL, {
      attribution: MAP_ATTRIBUTION,
      subdomains: "abcd",
      maxZoom: 20,
    }).addTo(map);

    state.map = map;
    state.aircraftLayer = L.layerGroup().addTo(map);
    return map;
  }

  function aircraftDivIcon(ac, isSelected) {
    const color = isSelected ? COLORS.selected : (ac.on_ground ? COLORS.headingGround : COLORS.heading);
    const rot = ac.track || 0;
    const html = `<svg width="20" height="20" viewBox="-10 -10 20 20" style="overflow:visible;transform:rotate(${rot}deg);">
      <polygon points="0,-8 5,6 0,3 -5,6" fill="${color}" stroke="#000" stroke-width="0.6"></polygon>
    </svg>`;
    return L.divIcon({ html, className: "plane-divicon", iconSize: [20, 20], iconAnchor: [10, 10] });
  }

  function aircraftTooltipHtml(ac, isSelected) {
    const altText = ac.on_ground ? "GND" : (ac.alt_baro != null ? `${ac.alt_baro} ft` : "");
    return `<div class="${isSelected ? "plane-tip-selected" : ""}">
      <div class="plane-tip-call">${routeTag(ac)}</div>
      ${ac.type ? `<div class="plane-tip-type">${ac.type}</div>` : ""}
      ${altText ? `<div class="plane-tip-alt">${altText}</div>` : ""}
    </div>`;
  }

  function renderMap(aircraftList, rangeNm, queryDistNm) {
    if (state.lat == null || state.lon == null) return;
    const map = initMap();
    const center = [state.lat, state.lon];

    // home marker
    if (!state.homeMarker) {
      state.homeMarker = L.circleMarker(center, {
        radius: 5, color: COLORS.home, weight: 2, fillColor: COLORS.home, fillOpacity: 1,
      }).addTo(map);
    } else {
      state.homeMarker.setLatLng(center);
    }
    state.homeMarker.unbindTooltip().bindTooltip(state.locationLabel, {
      permanent: true, direction: "top", className: "home-tooltip", offset: [0, -6],
    });

    // query-radius circle
    const queryMeters = rangeNm * NM_TO_M;
    if (!state.mapQueryCircle) {
      state.mapQueryCircle = L.circle(center, {
        radius: queryMeters, color: COLORS.ring, weight: 1.5, fillOpacity: 0.03,
      }).addTo(map);
    } else {
      state.mapQueryCircle.setLatLng(center).setRadius(queryMeters);
    }

    // adsb.fi data-limit boundary, only when it's smaller than the display range
    if (queryDistNm != null && queryDistNm < rangeNm) {
      const limitMeters = queryDistNm * NM_TO_M;
      if (!state.mapLimitCircle) {
        state.mapLimitCircle = L.circle(center, {
          radius: limitMeters, color: COLORS.limitRing, weight: 1.5, dashArray: "6 5", fillOpacity: 0,
        }).addTo(map);
      } else {
        state.mapLimitCircle.setLatLng(center).setRadius(limitMeters).addTo(map);
      }
    } else if (state.mapLimitCircle) {
      map.removeLayer(state.mapLimitCircle);
    }

    // aircraft markers - update in place where possible for smooth movement
    const seenHex = new Set();
    for (const ac of aircraftList) {
      if (ac.lat == null || ac.lon == null) continue;
      seenHex.add(ac.hex);
      const isSelected = ac.hex === state.selectedHex;
      const latlng = [ac.lat, ac.lon];
      let marker = state.mapMarkers.get(ac.hex);
      if (!marker) {
        marker = L.marker(latlng, { icon: aircraftDivIcon(ac, isSelected) });
        marker.on("click", () => selectAircraft(ac));
        marker.addTo(state.aircraftLayer);
        state.mapMarkers.set(ac.hex, marker);
      } else {
        marker.setLatLng(latlng);
        marker.setIcon(aircraftDivIcon(ac, isSelected));
      }
      marker.unbindTooltip().bindTooltip(aircraftTooltipHtml(ac, isSelected), {
        permanent: true, direction: "right", offset: [8, 0], className: "plane-tooltip",
      });
    }

    // drop markers for aircraft no longer in the filtered list
    for (const [hex, marker] of state.mapMarkers) {
      if (!seenHex.has(hex)) {
        state.aircraftLayer.removeLayer(marker);
        state.mapMarkers.delete(hex);
      }
    }
  }

  function recenterMap(zoom) {
    if (state.map && state.lat != null && state.lon != null) {
      state.map.setView([state.lat, state.lon], zoom != null ? zoom : state.map.getZoom());
    }
  }

  function renderCurrentView(aircraftList, rangeNm, queryDistNm) {
    if (state.viewMode === "map") {
      renderMap(aircraftList, rangeNm, queryDistNm);
    } else {
      drawRadar(aircraftList, rangeNm, queryDistNm);
    }
  }

  function setViewMode(mode) {
    state.viewMode = mode;
    els.viewRadarBtn.classList.toggle("active", mode === "radar");
    els.viewMapBtn.classList.toggle("active", mode === "map");
    els.radarBezel.classList.toggle("hidden", mode !== "radar");
    els.mapBezel.classList.toggle("hidden", mode !== "map");
    saveLastLocation();

    if (mode === "map") {
      initMap();
      requestAnimationFrame(() => {
        state.map.invalidateSize();
        applyFiltersAndRender();
      });
    } else {
      applyFiltersAndRender();
    }
  }

  els.viewRadarBtn.addEventListener("click", () => setViewMode("radar"));
  els.viewMapBtn.addEventListener("click", () => setViewMode("map"));

  // ---------- zoom ----------

  let rangeRefetchTimer = null;

  function clampRange(r) {
    return Math.min(MAX_RANGE_NM, Math.max(MIN_RANGE_NM, r));
  }

  function niceRange(r) {
    // keep small ranges at half-nm precision, larger ranges as whole nm
    return r < 10 ? Math.round(r * 2) / 2 : Math.round(r);
  }

  function setRange(newRange) {
    const clamped = niceRange(clampRange(newRange));
    if (clamped === state.rangeNm) return;
    state.rangeNm = clamped;
    els.rangeValue.textContent = `${state.rangeNm} nm`;
    // redraw immediately from the last known aircraft so zoom feels responsive,
    // then refetch from the server (debounced) since the query radius changed
    if (state.lastAircraftByHex.size) applyFiltersAndRender();
    saveLastLocation();
    clearTimeout(rangeRefetchTimer);
    rangeRefetchTimer = setTimeout(refreshAircraft, RANGE_REFETCH_DEBOUNCE_MS);
  }

  function zoomBy(factor) {
    setRange(state.rangeNm * factor);
  }

  els.canvas.addEventListener("wheel", (evt) => {
    evt.preventDefault();
    zoomBy(evt.deltaY < 0 ? 0.85 : 1.18); // scroll up = zoom in (smaller range)
  }, { passive: false });

  els.zoomInBtn.addEventListener("click", () => zoomBy(0.75));
  els.zoomOutBtn.addEventListener("click", () => zoomBy(1.35));

  function touchDist(touches) {
    const dx = touches[0].clientX - touches[1].clientX;
    const dy = touches[0].clientY - touches[1].clientY;
    return Math.hypot(dx, dy);
  }

  let pinchStartDist = null;
  let pinchStartRange = null;

  els.canvas.addEventListener("touchstart", (evt) => {
    if (evt.touches.length === 2) {
      pinchStartDist = touchDist(evt.touches);
      pinchStartRange = state.rangeNm;
    }
  }, { passive: true });

  els.canvas.addEventListener("touchmove", (evt) => {
    if (evt.touches.length === 2 && pinchStartDist) {
      evt.preventDefault();
      const d = touchDist(evt.touches);
      setRange(pinchStartRange * (pinchStartDist / d));
    }
  }, { passive: false });

  els.canvas.addEventListener("touchend", (evt) => {
    if (evt.touches.length < 2) pinchStartDist = null;
  });

  // ---------- altitude filter ----------

  els.altFilterBtn.addEventListener("click", (evt) => {
    evt.stopPropagation();
    els.altFilterMenu.classList.toggle("hidden");
  });

  document.addEventListener("click", (evt) => {
    if (!els.altFilterMenu.classList.contains("hidden") && !els.altFilterMenu.contains(evt.target) && evt.target !== els.altFilterBtn) {
      els.altFilterMenu.classList.add("hidden");
    }
  });

  function altBandCheckboxes() {
    return Array.from(els.altFilterMenu.querySelectorAll("input[data-band]"));
  }

  function onAltFilterChange() {
    state.activeAltBands = new Set(
      altBandCheckboxes().filter((cb) => cb.checked).map((cb) => cb.dataset.band)
    );
    saveLastLocation();
    applyFiltersAndRender();
  }

  for (const cb of altBandCheckboxes()) {
    cb.addEventListener("change", onAltFilterChange);
  }

  els.altFilterAll.addEventListener("click", () => {
    for (const cb of altBandCheckboxes()) cb.checked = true;
    onAltFilterChange();
  });

  els.altFilterNone.addEventListener("click", () => {
    for (const cb of altBandCheckboxes()) cb.checked = false;
    onAltFilterChange();
  });

  els.hideGroundTraffic.addEventListener("change", () => {
    state.hideGroundTraffic = els.hideGroundTraffic.checked;
    saveLastLocation();
    applyFiltersAndRender();
  });

  // ---------- callsign / registration filters ----------

  function onCallsignFilterChange() {
    state.callsignPrefixes = parsePrefixListInput(els.callsignFilter.value);
    saveLastLocation();
    applyFiltersAndRender();
  }

  function onRegistrationFilterChange() {
    state.registrationPrefixes = parsePrefixListInput(els.registrationFilter.value);
    saveLastLocation();
    applyFiltersAndRender();
  }

  els.callsignFilter.addEventListener("input", onCallsignFilterChange);
  els.registrationFilter.addEventListener("input", onRegistrationFilterChange);

  // ---------- click-to-select ----------

  function canvasEventToLogicalXY(evt) {
    const rect = els.canvas.getBoundingClientRect();
    const scaleX = els.canvas.width / rect.width;
    const scaleY = els.canvas.height / rect.height;
    return {
      x: (evt.clientX - rect.left) * scaleX,
      y: (evt.clientY - rect.top) * scaleY,
    };
  }

  els.canvas.addEventListener("click", (evt) => {
    const { x, y } = canvasEventToLogicalXY(evt);
    let best = null, bestDist = Infinity;
    for (const m of state.lastMarkers) {
      const d = Math.hypot(m.x - x, m.y - y);
      if (d < bestDist && d < HIT_RADIUS_PX) { bestDist = d; best = m; }
    }
    if (best) selectAircraft(best.ac);
  });

  els.detailsClose.addEventListener("click", closeDetails);

  function closeDetails() {
    state.selectedHex = null;
    els.detailsPanel.classList.add("hidden");
    for (const tr of els.aircraftBody.querySelectorAll("tr.selected")) tr.classList.remove("selected");
  }

  function fmtAlt(ac) {
    if (ac.on_ground) return "On ground";
    if (ac.alt_baro == null) return "--";
    return `${ac.alt_baro.toLocaleString()} ft`;
  }

  function fmtGs(ac) {
    return ac.gs != null ? `${Math.round(ac.gs)} kt` : "--";
  }

  function fmtTrack(ac) {
    return ac.track != null ? `${Math.round(ac.track)}°` : "--";
  }

  function fmtDist(ac) {
    return `${ac.distance_nm.toFixed(1)} nm @ ${Math.round(ac.bearing)}°`;
  }

  function airportLabel(info, icao) {
    if (!info) return icao || "?";
    return `${icao} · ${info.name}`;
  }

  function selectAircraft(ac) {
    state.selectedHex = ac.hex;
    els.detailsPanel.classList.remove("hidden");

    // highlight matching row
    for (const tr of els.aircraftBody.querySelectorAll("tr")) {
      tr.classList.toggle("selected", tr.dataset.hex === ac.hex);
    }

    renderDetailsStatic(ac);
    populateRoute(ac);

    // redraw immediately so the selection ring appears without waiting for the next poll
    if (state.lastAircraftByHex.size) applyFiltersAndRender();
  }

  function renderDetailsStatic(ac) {
    const title = ac.flight || ac.reg || ac.hex;
    els.detailsTitle.textContent = title;
    els.detailsType.textContent = ac.type ? `${ac.type}${ac.desc ? " — " + ac.desc : ""}` : "--";
    els.detailsReg.textContent = ac.reg || "--";
    els.detailsAlt.textContent = fmtAlt(ac);
    els.detailsGs.textContent = fmtGs(ac);
    els.detailsTrack.textContent = fmtTrack(ac);
    els.detailsDist.textContent = fmtDist(ac);
    els.detailsSquawk.textContent = ac.squawk || "--";
  }

  function refreshDetailsLiveFields() {
    const ac = state.lastAircraftByHex.get(state.selectedHex);
    if (!ac) {
      els.detailsAlt.textContent += " (last known)";
      return;
    }
    els.detailsAlt.textContent = fmtAlt(ac);
    els.detailsGs.textContent = fmtGs(ac);
    els.detailsTrack.textContent = fmtTrack(ac);
    els.detailsDist.textContent = fmtDist(ac);
    els.detailsSquawk.textContent = ac.squawk || "--";
  }

  async function populateRoute(ac) {
    if (!ac.flight) {
      els.detailsRoute.textContent = "No callsign broadcast";
      return;
    }
    els.detailsRoute.textContent = "Looking up...";
    let entry = state.routeCache.get(ac.flight);
    if (!entry || Date.now() - entry.ts > 6 * 3600 * 1000) {
      entry = await fetchRoute(ac.flight);
    }
    // bail if the user selected something else while this was in flight
    if (state.selectedHex !== ac.hex) return;

    if (!entry || !entry.origin || !entry.destination) {
      els.detailsRoute.textContent = "No route data";
      return;
    }
    const [originInfo, destInfo] = await Promise.all([
      getAirportInfo(entry.origin),
      getAirportInfo(entry.destination),
    ]);
    if (state.selectedHex !== ac.hex) return;
    const originLabel = airportLabel(originInfo, entry.origin);
    const destLabel = airportLabel(destInfo, entry.destination);
    els.detailsRoute.innerHTML = `${originLabel}<br>&rarr; ${destLabel}`;
  }

  // ---------- clock ----------

  function tickClock() {
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");
    const mon = now.toLocaleString("en-US", { month: "short" }).toUpperCase();
    els.footerTime.textContent = `${hh}:${mm} ${day} ${mon}`;
    els.footerLoc.textContent = state.locationLabel || "--";
  }
  setInterval(tickClock, 1000);
  tickClock();

  // ---------- wiring ----------

  els.goBtn.addEventListener("click", () => goToIcao(els.icaoInput.value));
  els.icaoInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") goToIcao(els.icaoInput.value);
  });
  els.geoBtn.addEventListener("click", useMyLocation);
  els.weatherToggle.addEventListener("change", refreshWeather);

  // initial state: restore last session, else default to KRDU at DEFAULT_RANGE_NM
  const last = loadLastLocation();
  if (last && last.rangeNm) state.rangeNm = niceRange(clampRange(last.rangeNm));
  els.rangeValue.textContent = `${state.rangeNm} nm`;

  if (last && Array.isArray(last.altBands)) {
    state.activeAltBands = new Set(last.altBands);
    for (const cb of altBandCheckboxes()) {
      cb.checked = state.activeAltBands.has(cb.dataset.band);
    }
  }

  if (last && last.hideGroundTraffic) {
    state.hideGroundTraffic = true;
    els.hideGroundTraffic.checked = true;
  }

  if (last && Array.isArray(last.callsignPrefixes) && last.callsignPrefixes.length) {
    state.callsignPrefixes = last.callsignPrefixes;
    els.callsignFilter.value = last.callsignPrefixes.join(", ");
  }

  if (last && Array.isArray(last.registrationPrefixes) && last.registrationPrefixes.length) {
    state.registrationPrefixes = last.registrationPrefixes;
    els.registrationFilter.value = last.registrationPrefixes.join(", ");
  }

  if (last && last.viewMode === "map") {
    setViewMode("map");
  }

  if (last && last.mode === "icao" && last.icao) {
    els.icaoInput.value = last.icao;
    goToIcao(last.icao);
  } else if (last && last.mode === "latlon" && last.lat != null) {
    goToLatLon(last.lat, last.lon, "MY LOCATION");
  } else {
    els.icaoInput.value = "KRDU";
    goToIcao("KRDU");
  }
})();
"""


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/static/style.css")
def static_style_css():
    return Response(STYLE_CSS, mimetype="text/css")


@app.route("/static/radar.js")
def static_radar_js():
    return Response(RADAR_JS, mimetype="application/javascript")


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
