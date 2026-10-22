package com.voiceflock.app

import android.content.Context
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/** Pulls the gallery JSON and the ONNX model from the laptop. Works on LAN or Tailscale; offline afterwards. */
object Sync {
    class SyncError(msg: String) : Exception(msg)

    private fun open(prefs: Prefs, path: String, method: String = "GET"): HttpURLConnection {
        val c = URL(prefs.serverUrl + path).openConnection() as HttpURLConnection
        c.requestMethod = method
        c.connectTimeout = 15000
        c.readTimeout = 120000
        if (prefs.password.isNotEmpty()) {
            val tok = Base64.encodeToString("phone:${prefs.password}".toByteArray(), Base64.NO_WRAP)
            c.setRequestProperty("Authorization", "Basic $tok")
        }
        return c
    }

    suspend fun run(context: Context, prefs: Prefs, status: (String) -> Unit): Gallery = withContext(Dispatchers.IO) {
        status("downloading voice library…")
        val gc = open(prefs, "/api/mobile/gallery.json")
        if (gc.responseCode == 401) throw SyncError("password rejected")
        if (gc.responseCode != 200) throw SyncError("server replied ${gc.responseCode} for gallery")
        val text = gc.inputStream.bufferedReader().readText()
        val gallery = Gallery.parse(text)
        Gallery.file(context).writeText(text)
        status("library: ${gallery.people.size} voices, ${gallery.printCount} prints")

        val model = Gallery.modelFile(context)
        var attempts = 0
        while (true) {
            val mc = open(prefs, "/api/mobile/model.onnx")
            val code = mc.responseCode
            if (code == 200) {
                status("downloading speaker model (≈85 MB, once)…")
                val tmp = File(model.path + ".part")
                mc.inputStream.use { inp -> tmp.outputStream().use { out ->
                    val buf = ByteArray(1 shl 16)
                    var total = 0L
                    while (true) {
                        val n = inp.read(buf); if (n < 0) break
                        out.write(buf, 0, n); total += n
                        if (total % (4L shl 20) < (1 shl 16)) status("model: ${total shr 20} MB")
                    }
                } }
                tmp.renameTo(model)
                break
            } else if (code == 202) {
                if (attempts == 0) try { open(prefs, "/api/mobile/export_model", "POST").responseCode } catch (_: Exception) {}
                attempts++
                if (attempts > 60) throw SyncError("the laptop is still exporting the model; try again in a minute")
                status("laptop is exporting the model… (${attempts * 5}s)")
                delay(5000)
            } else throw SyncError("server replied $code for model")
        }
        prefs.lastSync = java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.US).format(java.util.Date())
        status("synced ${gallery.people.size} voices · model ${model.length() shr 20} MB")
        gallery
    }
}
