package eu.vinyes.planeradar

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.webkit.GeolocationPermissions
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import fi.iki.elonen.NanoHTTPD
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.Collections

/**
 * Thin WebView shell around standalone.html (bundled as an asset - the same
 * self-contained radar/map app that runs in a desktop browser). No native
 * UI beyond the WebView itself; all app logic lives in the bundled HTML/JS.
 *
 * Also runs a minimal embedded HTTP server (see LanWebServer) so other
 * devices on the same WiFi network can open a browser and see the exact
 * same live radar, without installing anything themselves. The WebView
 * loads the app from that same local server (http://127.0.0.1:PORT/)
 * instead of file:///android_asset/... so both this device and any others
 * on the network are running identical code from identical URLs.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private var webServer: LanWebServer? = null
    private var pendingGeoOrigin: String? = null
    private var pendingGeoCallback: GeolocationPermissions.Callback? = null
    private var announcedLanUrl = false

    companion object {
        private const val LOCATION_PERMISSION_REQUEST_CODE = 1001
        private const val SERVER_PORT = 8765
        private const val TAG = "PlaneRadar"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        startWebServer()

        webView = WebView(this)
        setContentView(webView)

        val settings: WebSettings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true   // needed for the app's localStorage "remember last session"
        settings.databaseEnabled = true
        settings.setGeolocationEnabled(true)
        settings.cacheMode = WebSettings.LOAD_DEFAULT

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
                // Keep the app itself in the WebView; send anything else
                // (e.g. the OpenStreetMap/CARTO attribution links in the
                // map view) out to the system browser instead.
                return if (url.startsWith("http://127.0.0.1:$SERVER_PORT")) {
                    false
                } else {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                    true
                }
            }

            override fun onPageFinished(view: WebView, url: String) {
                super.onPageFinished(view, url)
                announceLanUrlOnce(view)
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onGeolocationPermissionsShowPrompt(
                origin: String,
                callback: GeolocationPermissions.Callback
            ) {
                if (hasLocationPermission()) {
                    callback.invoke(origin, true, false)
                } else {
                    pendingGeoOrigin = origin
                    pendingGeoCallback = callback
                    requestLocationPermission()
                }
            }
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })

        // Loaded from our own embedded server (not file://) so this device
        // and any other device on the network run from the same URL scheme.
        webView.loadUrl("http://127.0.0.1:$SERVER_PORT/")
    }

    private fun startWebServer() {
        try {
            webServer = LanWebServer(this, SERVER_PORT).apply {
                start(NanoHTTPD.SOCKET_READ_TIMEOUT, false)
            }
            Log.i(TAG, "LAN web server started on port $SERVER_PORT")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start LAN web server: ${e.message}")
            Toast.makeText(this, "Couldn't start the local web server (${e.message}) - the app will still work on this device, just not shareable over WiFi", Toast.LENGTH_LONG).show()
        }
    }

    private fun announceLanUrlOnce(view: WebView) {
        if (announcedLanUrl) return
        announcedLanUrl = true

        val ip = getLanIpAddress()
        val lanUrl = if (ip != null) "http://$ip:$SERVER_PORT" else null

        if (lanUrl != null) {
            Toast.makeText(this, "Also viewable on this WiFi network at $lanUrl", Toast.LENGTH_LONG).show()
        } else {
            Toast.makeText(this, "Couldn't detect a WiFi IP to share - make sure you're connected to a network", Toast.LENGTH_LONG).show()
        }

        // Small persistent on-screen badge (in addition to the toast, which
        // fades quickly) so the LAN address stays visible/discoverable.
        val label = lanUrl ?: "not connected to WiFi"
        val js = """
            (function() {
              var b = document.createElement('div');
              b.textContent = 'LAN: $label';
              b.style.cssText = 'position:fixed;bottom:4px;right:6px;z-index:99999;background:#0c1117;color:#33d17a;font:11px monospace;padding:3px 6px;border:1px solid #1c6e40;border-radius:4px;opacity:0.85;pointer-events:none;';
              document.body.appendChild(b);
            })();
        """.trimIndent()
        view.evaluateJavascript(js, null)
    }

    private fun getLanIpAddress(): String? {
        return try {
            val interfaces = Collections.list(NetworkInterface.getNetworkInterfaces())
            for (intf in interfaces) {
                if (!intf.isUp || intf.isLoopback) continue
                val addresses = Collections.list(intf.inetAddresses)
                for (addr in addresses) {
                    if (addr is Inet4Address && !addr.isLoopbackAddress) {
                        return addr.hostAddress
                    }
                }
            }
            null
        } catch (e: Exception) {
            null
        }
    }

    private fun hasLocationPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this, Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
    }

    private fun requestLocationPermission() {
        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION),
            LOCATION_PERMISSION_REQUEST_CODE
        )
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == LOCATION_PERMISSION_REQUEST_CODE) {
            val granted = grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED
            pendingGeoCallback?.invoke(pendingGeoOrigin ?: "", granted, false)
            pendingGeoOrigin = null
            pendingGeoCallback = null
        }
    }

    override fun onDestroy() {
        webServer?.stop()
        webServer = null
        super.onDestroy()
    }
}
