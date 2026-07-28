# Plane Radar

A local ADS-B radar, styled after [ironicbadger's ESP32-Plane-Radar](https://github.com/ironicbadger/ESP32-Plane-Radar) round display. Comes in two forms:

- **`app.py`** — a small Flask web app, opened at `localhost` in a browser. Recommended: it proxies the three upstream APIs server-side, so it always works regardless of any single browser's cross-origin policy. It's a single file — the page markup, CSS, and JS are embedded in it directly (no `templates/`/`static/` folders to carry along); copy just `app.py` and `requirements.txt` to another machine and it runs.
- **`standalone.html`** — a single self-contained HTML file with everything inlined. No install, no server: just open it in a browser (double-click it, or drag it into a tab). It talks to adsb.fi, airplanes.live, adsbdb, and Open-Meteo *directly from the browser*. adsb.fi is confirmed to **not** send CORS headers (verified: it returns HTTP 200 but no `Access-Control-Allow-Origin`, so browsers refuse to hand the response to JS), so direct requests will fail there; if adsb.fi's request fails outright, the same query is retried against airplanes.live before giving up. There's an off-by-default "CORS proxy fallback" checkbox in the header - turning it on retries blocked requests through the public proxy [api.allorigins.win](https://allorigins.win/), at the cost of routing your queried lat/lon/range through that third-party server (not under our control, no privacy or uptime guarantees, purely opt-in). Airport ICAO lookups have a ~300-airport list of major world airports built directly into the file instead (zero network dependency - KRDU, LEBL, etc. resolve instantly), with a best-effort background fetch of the full OurAirports database for broader coverage. For guaranteed reliability with no third-party proxy involved, use the Flask version, which proxies adsb.fi/adsbdb server-side.

Feature set is the same in both: ICAO / lat-lon / "use my location" input, 1-3000 nm zoom (scroll, pinch, or +/- buttons), an altitude band filter (with a "remove ground traffic" toggle), click-to-select aircraft details, a weather/clock footer, a **Radar / Map view toggle** (round radar display, or a real OpenStreetMap-based map), **own-ship tracking** (turns the display into a moving-map, track-up onboard instrument), and **traffic tracking** (recenter-and-follow any selected contact).

## Quick start (Flask version)

```bash
cd plane-radar
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:8765**.

- Type an airport ICAO code (e.g. `KRDU`, `EGLL`, `KJFK`) and hit **Go**, or click **Use my location** to center on your browser's GPS/IP location.
- Zoom the radar with your scroll wheel, a pinch gesture on touch screens, or the +/- buttons (defaults to 25 nm, 3-250 nm range).
- Click any aircraft (on the radar or in the list) to open a side panel with type, registration, route, altitude, ground speed, heading, and squawk.
- Use the "Altitude" dropdown, the "Callsign" box, and the "Registration" box (each comma-separated prefixes, e.g. `NJE, AF` or `N, G-`) to filter what's shown. All filters apply on top of each other.
- Zoom goes from 1 to 3000 nm, but the live-position APIs (see below) hard-cap actual queries at 250 nm (confirmed: requests beyond that return nothing). Zooming out past 250 nm just makes the display's scale wider - it won't reveal aircraft further away than 250 nm. When that's the case, a dashed amber ring is drawn at the real 250 nm boundary (labeled "data limit: 250nm" on the radar, a matching dashed circle on the map), and the status bar notes it too - so an empty outer area reads as an API limit, not a bug.
- If adsb.fi itself fails to respond (down, timeout, etc.), the app automatically retries the same query against [airplanes.live](https://airplanes.live/), an independent feeder network with the same data shape and the same 250 nm cap. This is redundancy, not a bigger radius - when it kicks in, the status bar says "(via airplanes.live fallback)".
- The `routes` toggle enriches callsigns into origin-destination tags (e.g. `BOS-IND`) via adsbdb; `weather` shows current conditions in the footer via Open-Meteo.
- **Radar / Map toggle**: "Radar" is the original round ESP32-style display. "Map" plants the same aircraft on a real OpenStreetMap-based map (dark tiles via CARTO, data &copy; OpenStreetMap contributors) with a solid ring showing the current query radius and, when applicable, the dashed 250 nm data-limit ring. The map is freely pannable/zoomable on its own - that doesn't refetch data. The underlying query stays anchored to your chosen center until you re-center (new ICAO, lat/lon, or "use my location"), which is when the map view also recenters and rezooms. Your last-used view is remembered between sessions.
- **Own ship tracking**: enter your aircraft's full callsign in the "Own ship" box next to an ICAO code and hit **Track** - it searches for that callsign near the ICAO, locks onto it, and from then on self-locates every poll from its own transponder echo (real position/track/altitude/groundspeed, not the phone's GPS). While active: range is limited to 5-50 nm, the altitude filter switches to an automatic own-altitude &plusmn;5,000 ft window (the manual band checkboxes grey out), the radar rotates to **track-up** (a proper turning compass, not just a fixed N at top) instead of north-up, and a right-hand panel shows your own Callsign/Track/FL/GS. Click **Stop** to return to normal. When you select another aircraft while tracking, its details panel gains Rel. Altitude, Closure rate (closing/opening/stable, in kt), and Rel. Track (&plusmn;&deg; from your own heading).
- **Traffic tracking**: independent of own-ship mode - select any aircraft and click **Track** in its details panel to recenter the display and have it follow that specific contact every poll (in map view, the viewport pans along with it too). Click **Track** again (now labeled **Stop**) to release it and return to your previous center. If own-ship tracking is also active, the traffic you're following wins the query center while your own-ship panel keeps updating regardless; activating own-ship tracking cancels any traffic-track lock.

First run downloads and caches a ~10 MB airport database into a `data/` folder next to `app.py`, so ICAO lookups work offline afterward. That folder is created automatically and doesn't need to be copied between machines either — a fresh `data/` gets rebuilt on first run wherever `app.py` is launched (just needs internet access that one time).

## Standalone version

Just open `standalone.html` in a browser — no `pip install`, no server. It's a single file you can copy anywhere (USB stick, another computer, a phone). ICAO airport lookups work offline out of the box (built-in list of ~300 major airports); it also tries, in the background, to fetch the full OurAirports database for smaller/regional airports, caching it in `localStorage` if that succeeds. That fetch is optional - if your browser blocks it, you still get every major airport plus lat/lon and "use my location" as always-working alternatives. It's at full feature parity with the Flask version, including own-ship tracking and traffic tracking - the own-ship callsign search uses the same CORS-proxy/airplanes.live fallback chain as every other request here, since there's no server to do that lookup for it.

## How it works

- `app.py` — Flask backend. Resolves ICAO → lat/lon, queries adsb.fi for aircraft within range (falling back to airplanes.live if adsb.fi itself fails to respond), computes bearing/distance from your chosen center, and proxies adsbdb route lookups and Open-Meteo weather. All upstream calls are cached server-side to stay within the public rate limits (1 req/s). The round radar UI itself (canvas-drawn rings/compass, aircraft as heading triangles with a speed vector, the map view, filters, footer) is embedded in the same file as the `INDEX_HTML`/`STYLE_CSS`/`RADAR_JS` constants, served directly instead of through `render_template`/a `static/` folder.
- If you're editing the UI: those constants are the source of truth for the Flask version. `standalone.html` has its own copy of the same UI/logic (it has to be self-contained for the browser-only version), so a UI change generally needs to land in both places.

## Data sources & terms

- [adsb.fi opendata](https://opendata.adsb.fi/) — live aircraft positions (primary source). Personal, non-commercial use only; consider [running a feeder](https://www.adsb.fi/) if you use this a lot.
- [airplanes.live](https://airplanes.live/) — live aircraft positions (automatic fallback, used only if adsb.fi itself fails to respond). Same data shape, same 250 nm cap, independent feeder network. Personal, non-commercial use only.
- [adsbdb.com](https://www.adsbdb.com/) — flight route / airline enrichment. (Note: this is a static reference database of aircraft/airline/route lookups, not a live-position source — it has no lat/lon/radius search of its own.)
- [OurAirports](https://ourairports.com/data/) — ICAO airport → coordinates.
- [Open-Meteo](https://open-meteo.com/) — current weather, no API key required.
- [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors (map data) + [CARTO](https://carto.com/attributions) (dark basemap tiles), via [Leaflet.js](https://leafletjs.com/) — the "Map" view. Both are credited in the map's attribution control, as required by their terms.

## Android app

- **`android/`** — a minimal Kotlin/Gradle project: one `MainActivity` that's just a full-screen `WebView` loading the bundled `standalone.html` (copied into `android/app/src/main/assets/www/`), with geolocation permission handling wired up so "use my location" works, and external links (the OSM/CARTO attribution) opening in the system browser instead of inside the app.
- **Serves the same radar to your whole WiFi network.** The app also runs a minimal embedded HTTP server ([NanoHTTPD](https://github.com/NanoHttpd/nanohttpd), see `LanWebServer.kt`) bound to all interfaces on port 8765, serving that same bundled `standalone.html`. The app's own WebView loads from `http://127.0.0.1:8765/` instead of `file://`, so this device and any other device on the same WiFi are running identical code from identical URLs. On launch, a toast and a small on-screen badge (bottom-right corner) show the phone's LAN address (e.g. `http://192.168.1.42:8765`) - anyone else on that network can open it in their own browser, no install needed. This requires `android:usesCleartextTraffic="true"` in the manifest (Android blocks plain-HTTP by default from targetSdk 28+; our own loopback request needs the opt-in even though every actual data request the page makes is HTTPS). The server only runs while the app is open in the foreground (stops in `onDestroy`) - there's no background service.
- **`.github/workflows/android-build.yml`** — builds a debug APK on GitHub Actions and uploads it as a downloadable artifact. There's no Android SDK/Gradle available in this environment to build or test it locally, so GitHub Actions' preconfigured Android runners do the actual compiling; that CI run is the real first build/verification.
- To update the app after editing `standalone.html`, re-copy it into `android/app/src/main/assets/www/standalone.html` before pushing.
- Upgrading from an older build: since the WebView now loads from `http://127.0.0.1:8765` instead of `file://`, that's a different origin as far as the browser engine is concerned - any previously saved "last session" (`localStorage`) from an older APK won't carry over. It'll just re-save under the new origin on first use.

## Notes / next steps

- The Flask server binds to `0.0.0.0`, so it's also reachable from your phone at `http://<your-computer-ip>:8765` on the same network.
- To change the port: `PORT=9000 python app.py`.
- `standalone.html` also works as-is on an Android phone's browser (Chrome, Firefox, etc.) — open the file locally or host it anywhere static. From there, "Add to Home Screen" gives you an app-like icon and full-screen launch without any packaging.
- The Android app's embedded server also uses port 8765, matching the Flask version, purely for consistency - the two aren't related and don't need to agree.
