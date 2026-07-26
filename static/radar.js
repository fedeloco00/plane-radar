(() => {
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
  };

  function altitudeVisible(ac) {
    if (ac.on_ground) return state.activeAltBands.has("ground-5000");
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
      viewMode: state.viewMode,
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
    const tx = x + (west ? size * 0.018 : -size * 0.018);
    let ty = y - size * 0.02;
    const lineH = size * 0.024;
    const fontSize = Math.round(size * 0.02);

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
