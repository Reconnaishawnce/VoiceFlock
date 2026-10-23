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

    /** Public copy of the exported speaker model (same bytes the laptop serves). */
    const val MODEL_URL = "https://github.com/Reconnaishawnce/VoiceFlock/releases/download/model-ecapa-v1/ecapa_voxceleb.onnx"

    /** Save a library file received by WhatsApp/email/file picker. Returns the parsed gallery. */
    fun importLibrary(context: Context, text: String): Gallery {
        val g = Gallery.parse(text)            // throws if it is not a VoiceFlock library
        Gallery.file(context).writeText(text)
        return g
    }

    /** Turn a share link into a direct-download link. */
    fun normaliseLibraryUrl(raw: String): String {
        var u = raw.trim()
        if (u.contains("dropbox.com")) { u = u.replace("www.dropbox.com", "dl.dropboxusercontent.com").replace("?dl=0", "").replace("&dl=0", "").replace("?dl=1", "").replace("&dl=1", "") }
        Regex("drive\\.google\\.com/file/d/([^/]+)").find(u)?.let { u = "https://drive.google.com/uc?export=download&id=${it.groupValues[1]}" }
        Regex("github\\.com/([^/]+)/([^/]+)/blob/(.+)").find(u)?.let { u = "https://raw.githubusercontent.com/${it.groupValues[1]}/${it.groupValues[2]}/${it.groupValues[3]}" }
        return u
    }

    /** Fetch the library JSON from the user's link. Returns null when unchanged (ETag). */
    suspend fun fetchLibraryUrl(context: Context, prefs: Prefs, force: Boolean = false): Gallery? = withContext(Dispatchers.IO) {
        val url = normaliseLibraryUrl(prefs.libraryUrl)
        if (url.isEmpty()) throw SyncError("no library URL set")
        var cur = URL(url); var c: HttpURLConnection; var hops = 0
        while (true) {
            c = cur.openConnection() as HttpURLConnection
            c.instanceFollowRedirects = false; c.connectTimeout = 15000; c.readTimeout = 60000
            if (!force && prefs.libraryEtag.isNotEmpty()) c.setRequestProperty("If-None-Match", prefs.libraryEtag)
            val code = c.responseCode
            if (code in 300..399 && hops < 6) { cur = URL(cur, c.getHeaderField("Location")); hops++; continue }
            if (code == 304) return@withContext null
            if (code != 200) throw SyncError("library link replied HTTP $code")
            break
        }
        val text = c.inputStream.bufferedReader().readText()
        val g = Gallery.parse(text)
        Gallery.file(context).writeText(text)
        prefs.libraryEtag = c.getHeaderField("ETag") ?: ""
        prefs.lastSync = "link " + java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.US).format(java.util.Date())
        g
    }

    /** Download the model from GitHub (no laptop needed). */
    suspend fun downloadModelFromInternet(context: Context, status: (String) -> Unit): File = withContext(Dispatchers.IO) {
        val model = Gallery.modelFile(context)
        var url = URL(MODEL_URL)
        var c: HttpURLConnection
        var hops = 0
        while (true) {                           // follow GitHub's redirect to the CDN by hand (http->https safe)
            c = url.openConnection() as HttpURLConnection
            c.instanceFollowRedirects = false
            c.connectTimeout = 15000; c.readTimeout = 120000
            val code = c.responseCode
            if (code in 300..399 && hops < 5) { url = URL(url, c.getHeaderField("Location")); hops++; continue }
            if (code != 200) throw SyncError("model download failed: HTTP $code")
            break
        }
        status("downloading speaker model (≈85 MB, once)…")
        val tmp = File(model.path + ".part")
        c.inputStream.use { inp -> tmp.outputStream().use { out ->
            val buf = ByteArray(1 shl 16)
            var total = 0L
            while (true) {
                val n = inp.read(buf); if (n < 0) break
                out.write(buf, 0, n); total += n
                if (total % (4L shl 20) < (1 shl 16)) status("model: ${total shr 20} MB")
            }
        } }
        if (tmp.length() < 10_000_000) { tmp.delete(); throw SyncError("model download incomplete") }
        tmp.renameTo(model)
        status("model ready (${model.length() shr 20} MB)")
        model
    }

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
