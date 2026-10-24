package com.voiceflock.app

import android.content.Context
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import okhttp3.MediaType.Companion.toMediaTypeOrNull

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

    fun connected(prefs: Prefs) = prefs.serverUrl.isNotBlank() && !prefs.serverUrl.contains("192.168.1.10:8787")

    private fun post(prefs: Prefs, path: String, body: org.json.JSONObject): org.json.JSONObject {
        val c = open(prefs, path, "POST")
        c.doOutput = true; c.setRequestProperty("Content-Type", "application/json")
        c.outputStream.use { it.write(body.toString().toByteArray()) }
        val code = c.responseCode
        if (code == 401) throw SyncError("password rejected")
        val text = (if (code < 400) c.inputStream else c.errorStream)?.bufferedReader()?.readText() ?: ""
        if (code >= 400) throw SyncError("laptop replied $code: ${text.take(200)}")
        return org.json.JSONObject(text.ifBlank { "{}" })
    }

    private fun getJson(prefs: Prefs, path: String): org.json.JSONObject {
        val c = open(prefs, path)
        val code = c.responseCode
        if (code == 401) throw SyncError("password rejected")
        if (code != 200) throw SyncError("laptop replied $code")
        return org.json.JSONObject(c.inputStream.bufferedReader().readText())
    }

    /** Is the laptop there? Returns a one-line description (version, device, Tailscale name). */
    suspend fun health(prefs: Prefs): String = withContext(Dispatchers.IO) {
        val h = getJson(prefs, "/api/health")
        val tsj = h.optJSONObject("tailscale")
        val ts = tsj?.optString("dns_name", "")?.ifEmpty { null }
        prefs.laptopWebUrl = tsj?.optString("https_url", "")?.ifEmpty { null } ?: prefs.serverUrl
        prefs.lastLaptop = "VoiceFlock ${h.optString("version")} · ${h.optString("device")}" + (ts?.let { " · Tailscale $it" } ?: "")
        prefs.lastLaptop
    }

    private val http by lazy { okhttp3.OkHttpClient.Builder().connectTimeout(15, java.util.concurrent.TimeUnit.SECONDS).readTimeout(180, java.util.concurrent.TimeUnit.SECONDS).build() }
    private fun authHeader(prefs: Prefs): String? = if (prefs.password.isNotEmpty()) "Basic " + Base64.encodeToString("phone:${prefs.password}".toByteArray(), Base64.NO_WRAP) else null

    data class LaptopIdentify(val top: List<Match>, val seconds: Float, val gallery: Int, val threshold: Float, val did: String?)

    /** Send a clip to the laptop: full model, whole library. action = "add:<id>" | "enroll:<name>" | null. */
    suspend fun identify(prefs: Prefs, pcm: ShortArray, action: String? = null): LaptopIdentify = withContext(Dispatchers.IO) {
        val bb = java.nio.ByteBuffer.allocate(pcm.size * 2).order(java.nio.ByteOrder.LITTLE_ENDIAN); for (v in pcm) bb.putShort(v)
        val body = okhttp3.MultipartBody.Builder().setType(okhttp3.MultipartBody.FORM)
            .addFormDataPart("audio", "clip.pcm", okhttp3.RequestBody.create("application/octet-stream".toMediaTypeOrNull(), bb.array()))
            .addFormDataPart("device", prefs.deviceId)
        if (action != null) body.addFormDataPart("action", action)
        val rb = okhttp3.Request.Builder().url(prefs.serverUrl + "/api/identify").post(body.build())
        authHeader(prefs)?.let { rb.header("Authorization", it) }
        http.newCall(rb.build()).execute().use { r ->
            val text = r.body?.string() ?: ""
            if (r.code == 401) throw SyncError("password rejected")
            if (r.code != 200) throw SyncError("laptop replied ${r.code}: ${text.take(160)}")
            val j = org.json.JSONObject(text); val arr = j.getJSONArray("candidates"); val top = ArrayList<Match>()
            for (i in 0 until arr.length()) { val c = arr.getJSONObject(i)
                val p = Person(c.getInt("person_id"), c.getString("name"), c.optString("kind", "named"), c.optBoolean("alert", false), emptyList(),
                    c.optString("alert_sound", "").ifEmpty { null }.takeIf { it != "null" }, c.optString("alert_message", "").ifEmpty { null }.takeIf { it != "null" })
                top.add(Match(p, c.optDouble("raw_score", 0.0).toFloat(), c.optDouble("probability", 0.0).toFloat())) }
            val did = j.optJSONObject("did")?.let { d -> if (d.has("enrolled")) "Enrolled ${d.optString("name")} on the laptop" else "Added a print to ${d.optString("name")} on the laptop" }
            LaptopIdentify(top, j.optDouble("seconds", 0.0).toFloat(), j.optInt("gallery", 0), j.optDouble("threshold", 0.75).toFloat(), did)
        }
    }

    /** A kept alert clip becomes a recording on the laptop (Review queue), with a note saying where it came from. */
    suspend fun uploadClip(prefs: Prefs, file: java.io.File, name: String, note: String): Int = withContext(Dispatchers.IO) {
        val body = okhttp3.MultipartBody.Builder().setType(okhttp3.MultipartBody.FORM)
            .addFormDataPart("mode", "review")
            .addFormDataPart("files", "$name.wav", okhttp3.RequestBody.create("audio/wav".toMediaTypeOrNull(), file)).build()
        val rb = okhttp3.Request.Builder().url(prefs.serverUrl + "/api/upload").post(body)
        authHeader(prefs)?.let { rb.header("Authorization", it) }
        val sid = http.newCall(rb.build()).execute().use { r ->
            val text = r.body?.string() ?: ""
            if (r.code != 200) throw SyncError("laptop replied ${r.code}: ${text.take(160)}")
            val created = org.json.JSONObject(text).optJSONArray("created")
            if (created == null || created.length() == 0) throw SyncError("laptop did not accept the clip: ${text.take(160)}")
            val c = created.getJSONObject(0)
            if (c.has("duplicate_of")) throw SyncError("the laptop already has this clip")
            c.getInt("source_id")
        }
        try {
            val pb = okhttp3.RequestBody.create("application/json".toMediaTypeOrNull(), org.json.JSONObject().put("notes", note).toString())
            val prb = okhttp3.Request.Builder().url(prefs.serverUrl + "/api/sources/$sid").patch(pb)
            authHeader(prefs)?.let { prb.header("Authorization", it) }
            http.newCall(prb.build()).execute().close()
        } catch (_: Exception) {}
        sid
    }

    /** Share → VoiceFlock on a link: queue it on the laptop. */
    suspend fun queueUrl(prefs: Prefs, url: String, mode: String): String = withContext(Dispatchers.IO) {
        val r = post(prefs, "/api/upload_url", org.json.JSONObject().put("url", url).put("mode", mode))
        r.optString("name", "").ifEmpty { url }
    }

    /** Flags, sounds, lines and phone enrollments -> laptop. */
    suspend fun pushEdits(ctx: Context, prefs: Prefs): org.json.JSONObject = withContext(Dispatchers.IO) {
        val r = post(prefs, "/api/import/phone_settings", org.json.JSONObject(prefs.exportEdits(AppState.gallery.value)))
        prefs.editsDirty = false
        r
    }

    /** Laptop library -> phone when it changed. Returns the new gallery or null when unchanged. */
    suspend fun pullGallery(ctx: Context, prefs: Prefs): Gallery? = withContext(Dispatchers.IO) {
        val c = open(prefs, "/api/mobile/gallery.json")
        if (c.responseCode == 401) throw SyncError("password rejected")
        if (c.responseCode != 200) throw SyncError("laptop replied ${c.responseCode} for the library")
        val text = c.inputStream.bufferedReader().readText()
        val g = Gallery.parse(text)
        val cur = AppState.gallery.value
        if (cur != null && cur.generatedAt == g.generatedAt && cur.people.size == g.people.size) return@withContext null
        Gallery.file(ctx).writeText(text)
        prefs.lastSync = "laptop " + java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.US).format(java.util.Date())
        g
    }

    /** This phone's detections -> laptop timeline (only what wasn't sent yet). */
    suspend fun pushSightings(ctx: Context, prefs: Prefs): Int = withContext(Dispatchers.IO) {
        val mine = AppState.sightings.value.filter { it.device == "phone" && it.at > prefs.sightingsPushedMs }
        if (mine.isEmpty()) return@withContext 0
        val items = org.json.JSONArray()
        for (s in mine) items.put(org.json.JSONObject().put("kind", "phone").put("at_ms", s.at).put("person_id", if (s.personId > 0) s.personId else org.json.JSONObject.NULL)
            .put("name", s.name).put("probability", s.probability.toDouble()).put("flagged", s.flagged).put("session", s.session ?: org.json.JSONObject.NULL))
        post(prefs, "/api/sightings", org.json.JSONObject().put("device", prefs.deviceId).put("items", items))
        prefs.sightingsPushedMs = mine.maxOf { it.at }
        mine.size
    }

    /** Laptop timeline (Live mode + new recordings) -> phone. Returns the new ones. */
    suspend fun pullSightings(ctx: Context, prefs: Prefs): List<Sighting> = withContext(Dispatchers.IO) {
        val since = if (prefs.sightingsPulledMs > 0) prefs.sightingsPulledMs else System.currentTimeMillis() - 7L * 86_400_000
        val j = getJson(prefs, "/api/sightings?since_ms=$since&limit=500&exclude_device=${prefs.deviceId}")
        val arr = j.getJSONArray("sightings"); val out = ArrayList<Sighting>()
        for (i in 0 until arr.length()) { val x = arr.getJSONObject(i)
            val kind = x.optString("kind"); if (kind == "phone") continue
            out.add(Sighting(x.getLong("at_ms"), x.optInt("person_id", 0), x.getString("name"), x.optDouble("probability", 0.0).toFloat(), x.optInt("flagged", 0) == 1,
                if (kind == "live") "laptop" else "recording", x.optString("source_name", "").ifEmpty { null })) }
        if (out.isNotEmpty()) prefs.sightingsPulledMs = out.maxOf { it.at }
        else prefs.sightingsPulledMs = maxOf(prefs.sightingsPulledMs, j.optLong("now_ms", since) - 60_000)
        Sightings.addRemote(ctx, out)
    }

    /** Everything, each step on its own so one failure doesn't stop the rest. Returns a short summary. */
    suspend fun syncAll(ctx: Context, prefs: Prefs, force: Boolean = false): String {
        if (!connected(prefs)) return "no laptop address"
        if (!force && System.currentTimeMillis() - prefs.lastSyncMs < 45_000) return "recent"
        val parts = ArrayList<String>()
        try { if (prefs.editsDirty || LocalPrints.count(ctx) > 0) { val r = pushEdits(ctx, prefs); parts += "edits sent" + (r.optInt("enrollments", 0).takeIf { it > 0 }?.let { " ($it voices)" } ?: "") } } catch (e: Exception) { parts += "edits: ${e.message}" }
        try { pullGallery(ctx, prefs)?.let { reloadGallery(ctx); parts += "library ${it.people.size} voices" } } catch (e: Exception) { parts += "library: ${e.message}" }
        try { val n = pushSightings(ctx, prefs); if (n > 0) parts += "$n sightings sent" } catch (e: Exception) { parts += "sightings: ${e.message}" }
        try { val n = pullSightings(ctx, prefs); if (n.isNotEmpty()) parts += "${n.size} from laptop" } catch (e: Exception) { parts += "timeline: ${e.message}" }
        if (!Gallery.modelFile(ctx).exists()) try { downloadModelFromInternet(ctx) {} ; parts += "model ready" } catch (e: Exception) { parts += "model: ${e.message}" }
        prefs.lastSyncMs = System.currentTimeMillis()
        val line = parts.ifEmpty { listOf("in sync") }.joinToString(" · ")
        prefs.lastSync = "laptop " + java.text.SimpleDateFormat("HH:mm", java.util.Locale.US).format(java.util.Date()) + " · " + line
        return line
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
