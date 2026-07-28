package eu.vinyes.planeradar

import android.content.Context
import fi.iki.elonen.NanoHTTPD
import java.io.IOException

/**
 * Minimal embedded HTTP server: serves the bundled standalone.html (the same
 * self-contained radar/map app running in this device's own WebView) to any
 * browser on the same WiFi network. There's no native routing beyond "serve
 * the one HTML file" for every request - the page itself talks directly to
 * adsb.fi/airplanes.live/adsbdb/open-meteo from whichever browser loads it,
 * exactly like opening standalone.html locally on a desktop.
 *
 * NanoHTTPD binds to all interfaces by default (not just localhost), which
 * is what makes this reachable from other devices at http://<phone-ip>:port
 * - not just from the WebView on this same device.
 */
class LanWebServer(private val context: Context, port: Int) : NanoHTTPD(port) {

    override fun serve(session: IHTTPSession): Response {
        return try {
            val html = context.assets.open("www/standalone.html").bufferedReader().use { it.readText() }
            newFixedLengthResponse(Response.Status.OK, "text/html; charset=utf-8", html)
        } catch (e: IOException) {
            newFixedLengthResponse(
                Response.Status.INTERNAL_ERROR,
                "text/plain",
                "Failed to load standalone.html: ${e.message}"
            )
        }
    }
}
