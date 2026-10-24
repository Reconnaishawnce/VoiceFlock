package com.voiceflock.app

import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.concurrent.thread
import kotlin.math.log10
import kotlin.math.sqrt

/** What the Identify sheet shows. */
sealed class IdentifyState {
    data class Listening(val speechS: Float, val elapsedS: Float, val remote: Boolean = false) : IdentifyState()
    object Asking : IdentifyState()
    /** Local or laptop result. pcm is the raw clip so it can be re-sent ("Ask the laptop", add, enroll). */
    data class Result(val embedding: FloatArray?, val speechS: Float, val top: List<Match>, val pcm: ShortArray? = null, val remote: Boolean = false,
                      val gallery: Int = 0, val threshold: Float? = null) : IdentifyState()
    data class Failed(val message: String) : IdentifyState()
}

/** Foreground service: microphone -> matcher (on the phone, or the laptop's Live mode over the network) -> sightings + alerts. */
class ListenService : Service() {
    companion object {
        const val ACTION_START = "com.voiceflock.app.START"
        const val ACTION_STOP = "com.voiceflock.app.STOP"
        const val ACTION_IDENTIFY = "com.voiceflock.app.IDENTIFY"
        const val ACTION_IDENTIFY_CANCEL = "com.voiceflock.app.IDENTIFY_CANCEL"
        private const val SR = 16000
        private const val IDENTIFY_SPEECH_S = 6f
        private const val IDENTIFY_MAX_S = 15f
        private const val IDENTIFY_MIN_S = 1.5f
        private const val RING_S = 30

        fun start(context: Context) = send(context, ACTION_START)
        fun stop(context: Context) { context.startService(Intent(context, ListenService::class.java).setAction(ACTION_STOP)) }
        fun identify(context: Context) = send(context, ACTION_IDENTIFY)
        fun cancelIdentify(context: Context) { context.startService(Intent(context, ListenService::class.java).setAction(ACTION_IDENTIFY_CANCEL)) }
        private fun send(context: Context, action: String) {
            val i = Intent(context, ListenService::class.java).setAction(action)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(i) else context.startService(i)
        }

        fun writeWav(file: File, pcm: ShortArray, sr: Int = SR) {
            val bytes = pcm.size * 2
            RandomAccessFile(file, "rw").use { f ->
                f.setLength(0)
                val h = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN)
                h.put("RIFF".toByteArray()).putInt(36 + bytes).put("WAVE".toByteArray()).put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(1).putInt(sr).putInt(sr * 2).putShort(2).putShort(16).put("data".toByteArray()).putInt(bytes)
                f.write(h.array())
                val bb = ByteBuffer.allocate(bytes).order(ByteOrder.LITTLE_ENDIAN); for (v in pcm) bb.putShort(v); f.write(bb.array())
            }
        }
    }

    private class IdentifyReq(val startedAt: Long = System.currentTimeMillis()) { val frames = ArrayList<FloatArray>(); val raw = ArrayList<ShortArray>() }

    @Volatile private var running = false
    @Volatile private var oneShot = false
    @Volatile private var identifyReq: IdentifyReq? = null
    private var worker: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val lastSeenAt = HashMap<Int, Long>()
    private val ring = ShortArray(SR * RING_S); private var ringPos = 0; private var ringFilled = 0

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> { stopListening(); stopSelf(); return START_NOT_STICKY }
            ACTION_IDENTIFY_CANCEL -> {
                identifyReq = null; AppState.identify.value = null
                if (oneShot) { stopListening(); stopSelf() }
                return START_NOT_STICKY
            }
        }
        val wantIdentify = intent?.action == ACTION_IDENTIFY
        if (running) {
            if (wantIdentify) { identifyReq = IdentifyReq(); AppState.identify.value = IdentifyState.Listening(0f, 0f, AppState.mode.value == "laptop") }
            return START_STICKY
        }
        Alerts.ensureChannels(this)
        Alerts.initTts(this)
        val notif = Alerts.serviceNotification(this, if (wantIdentify) "identifying a voice…" else "starting…")
        if (Build.VERSION.SDK_INT >= 29) startForeground(Alerts.NOTIF_SERVICE, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        else startForeground(Alerts.NOTIF_SERVICE, notif)
        oneShot = wantIdentify
        if (wantIdentify) { identifyReq = IdentifyReq(); AppState.identify.value = IdentifyState.Listening(0f, 0f) }
        startListening()
        return START_STICKY
    }

    private fun ringWrite(pcm: ShortArray, n: Int) {
        for (i in 0 until n) { ring[ringPos] = pcm[i]; ringPos = (ringPos + 1) % ring.size }
        ringFilled = minOf(ring.size, ringFilled + n)
    }
    private fun ringSnapshot(): ShortArray {
        val out = ShortArray(ringFilled)
        val start = if (ringFilled < ring.size) 0 else ringPos
        for (i in 0 until ringFilled) out[i] = ring[(start + i) % ring.size]
        return out
    }

    /** Sightings log + alarms, the same whether the frame came from the phone's matcher or the laptop. */
    private fun handleDetection(d: Detection, prefs: Prefs, g: Gallery, threshold: Float) {
        AppState.detection.value = d
        val top = d.top.firstOrNull() ?: return
        if (oneShot || !d.speech) return
        if (top.probability >= threshold && (top.person.kind == "named" || top.flagged)) {
            val now = System.currentTimeMillis()
            if (now - (lastSeenAt[top.person.id] ?: 0L) > prefs.cooldownSeconds * 1000L) {
                lastSeenAt[top.person.id] = now
                var clipPath: String? = null
                if (d.alert && prefs.keepAlertClips && ringFilled > SR * 3) {
                    try {
                        val dir = File(filesDir, "clips").apply { mkdirs() }
                        val f = File(dir, "alert-$now.wav"); writeWav(f, ringSnapshot()); clipPath = f.path
                    } catch (_: Exception) {}
                }
                Sightings.add(this, Sighting(now, top.person.id, top.person.name, top.probability, d.alert, clip = clipPath))
                if (d.alert) {
                    Alerts.voiceAlert(this, top.person, top.probability)
                    Alerts.performAlert(this, top.person.name, prefs.soundFor(top.person, g), prefs.messageFor(top.person, g), prefs.demoMode)
                }
            }
        }
    }

    private fun startListening() {
        val prefs = Prefs(this)
        val gallery0 = Gallery.load(this)
        val model = Gallery.modelFile(this)
        val wantRemote = prefs.processing == "laptop" && Sync.connected(prefs)
        if (gallery0 == null || (!model.exists() && !wantRemote)) {
            val msg = if (gallery0 == null) "No voice library yet: import one in Settings." else "Speaker model not downloaded yet (Settings)."
            AppState.status.value = msg
            if (identifyReq != null) AppState.identify.value = IdentifyState.Failed(msg)
            identifyReq = null
            stopForegroundCompat(); stopSelf(); return
        }
        AppState.gallery.value = gallery0
        running = true
        AppState.listening.value = true
        AppState.listeningSince.value = System.currentTimeMillis()
        AppState.speechSeconds.value = 0f
        AppState.status.value = if (wantRemote) "connecting to the laptop…" else "loading model…"
        ListenTile.refresh(this)
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "voiceflock:listen").also { it.acquire() }
        val nm = getSystemService(NotificationManager::class.java)

        worker = thread(name = "voiceflock-listen") {
            var embedder: Embedder? = null
            var record: AudioRecord? = null
            var remote: RemoteLive? = null
            try {
                var g: Gallery = gallery0!!
                fun threshold() = if (prefs.thresholdOverride > 0f) prefs.thresholdOverride else (remote?.threshold ?: g.threshold)
                // --- laptop mode: open the stream first; fall back to the phone if it does not answer
                if (wantRemote) {
                    val r = RemoteLive(prefs, { d -> handleDetection(d, prefs, g, threshold()) }, { st -> AppState.status.value = "via laptop · $st" })
                    if (r.connect()) remote = r else { r.close(); AppState.status.value = "laptop not reachable (${r.failed ?: "timeout"}) · listening on the phone" }
                }
                AppState.mode.value = if (remote != null) "laptop" else "phone"
                var matcher: Matcher? = null
                fun newMatcher(): Matcher {
                    if (embedder == null) embedder = Embedder(model.path)
                    return Matcher(embedder!!, g, { p -> prefs.isFlagged(p, g) }, threshold()).also { m -> m.onSpeechFrame = { fr -> identifyReq?.frames?.add(fr) } }
                }
                if (remote == null) { if (!model.exists()) throw IllegalStateException("speaker model not downloaded and the laptop is not reachable"); matcher = newMatcher() }
                fun describe(): String { val nFlag = g.people.count { prefs.isFlagged(it, g) }; return (if (remote != null) "via laptop · ${remote!!.gallerySize} voices" else "$nFlag flagged · ${g.people.size} voices") }
                val minBuf = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
                record = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, SR))
                if (record.state != AudioRecord.STATE_INITIALIZED) throw IllegalStateException("microphone unavailable (another app holds it?)")
                record.startRecording()
                if (remote == null) AppState.status.value = "listening · ${describe()}"
                nm.notify(Alerts.NOTIF_SERVICE, Alerts.serviceNotification(this, describe()))
                val pcm = ShortArray(SR / 10)
                var lastNotif = 0L; var notifText = ""
                var lastSync = 0L
                val syncing = java.util.concurrent.atomic.AtomicBoolean(false)
                while (running) {
                    val n = record.read(pcm, 0, pcm.size)
                    if (n <= 0) continue
                    var acc = 0.0; for (i in 0 until n) { val v = pcm[i] / 32768.0; acc += v * v }
                    val db = 20 * log10(sqrt(acc / n) + 1e-9)
                    AppState.level.value = ((db + 60) / 50).toFloat().coerceIn(0f, 1f)
                    ringWrite(pcm, n)
                    identifyReq?.let { req -> if (remote != null || req.raw.size < 160) req.raw.add(pcm.copyOf(n)) }
                    val cur = AppState.gallery.value
                    if (cur != null && cur !== g) { g = cur; if (remote == null) matcher = newMatcher(); nm.notify(Alerts.NOTIF_SERVICE, Alerts.serviceNotification(this, describe())) }
                    val tnow = System.currentTimeMillis()
                    val rl = remote
                    if (rl != null) {
                        rl.send(pcm, n)
                        if (rl.failed != null) {            // the laptop went away: carry on locally if we can
                            rl.close(); remote = null; AppState.mode.value = "phone"
                            AppState.status.value = "laptop connection lost (${rl.failed}) · " + (if (model.exists()) "listening on the phone" else "no local model: stopping")
                            if (!model.exists()) { running = false; break }
                            matcher = newMatcher()
                        }
                    } else {
                        for (d in matcher!!.feed(pcm, n)) {
                            AppState.speechSeconds.value = matcher!!.speechSeconds
                            val top = d.top.firstOrNull()
                            val text = if (d.speech && top != null) "hearing ${top.person.name} · ${(top.probability * 100).toInt()}%" else describe()
                            if (text != notifText && tnow - lastNotif > 3000) { notifText = text; lastNotif = tnow; nm.notify(Alerts.NOTIF_SERVICE, Alerts.serviceNotification(this, text)) }
                            handleDetection(d, prefs, g, threshold())
                        }
                    }
                    identifyReq?.let { req -> stepIdentify(req, embedder, g, remote != null, prefs) }
                    if (oneShot && identifyReq == null) running = false
                    if (!oneShot && prefs.autoSync && Sync.connected(prefs) && tnow - lastSync > 30_000 && syncing.compareAndSet(false, true)) {
                        lastSync = tnow
                        thread(name = "voiceflock-sync") {
                            try {
                                kotlinx.coroutines.runBlocking {
                                    try { Sync.pushSightings(this@ListenService, prefs) } catch (_: Exception) {}
                                    val fresh = try { Sync.pullSightings(this@ListenService, prefs) } catch (_: Exception) { emptyList() }
                                    if (remote == null) for (s in fresh.filter { it.flagged && it.at > tnow - 10 * 60_000 }) Alerts.remoteNotification(this@ListenService, s)
                                    if (tnow - prefs.lastSyncMs > 5 * 60_000) try { Sync.pullGallery(this@ListenService, prefs)?.let { reloadGallery(this@ListenService) }; prefs.lastSyncMs = tnow } catch (_: Exception) {}
                                }
                            } finally { syncing.set(false) }
                        }
                    }
                }
            } catch (e: Exception) {
                AppState.status.value = "error: ${e.message}"
                identifyReq?.let { AppState.identify.value = IdentifyState.Failed(e.message ?: "error"); identifyReq = null }
            } finally {
                try { record?.stop() } catch (_: Exception) {}
                record?.release()
                remote?.close()
                embedder?.close()
                AppState.listening.value = false
                AppState.level.value = 0f
                AppState.mode.value = "phone"
                if (!AppState.status.value.startsWith("error")) AppState.status.value = "stopped"
                ListenTile.refresh(this)
                if (oneShot) { AppState.detection.value = null; wakeLock?.let { if (it.isHeld) it.release() }; wakeLock = null; stopForegroundCompat(); stopSelf() }
            }
        }
    }

    /** Collect speech until there is enough, then score the clip: on the phone, or on the laptop in laptop mode. */
    private fun stepIdentify(req: IdentifyReq, embedder: Embedder?, g: Gallery, remote: Boolean, prefs: Prefs) {
        val elapsed = (System.currentTimeMillis() - req.startedAt) / 1000f
        val speechS = if (remote) req.raw.size * 0.1f else req.frames.size * 0.1f
        val target = if (remote) 8f else IDENTIFY_SPEECH_S
        if (speechS < target && elapsed < IDENTIFY_MAX_S) { AppState.identify.value = IdentifyState.Listening(speechS, elapsed, remote); return }
        identifyReq = null
        if (remote) {
            val total = req.raw.sumOf { it.size }; val pcm = ShortArray(total); var off = 0
            for (ch in req.raw) { System.arraycopy(ch, 0, pcm, off, ch.size); off += ch.size }
            AppState.identify.value = IdentifyState.Asking
            thread(name = "voiceflock-identify") {
                try {
                    val r = kotlinx.coroutines.runBlocking { Sync.identify(prefs, pcm) }
                    AppState.identify.value = IdentifyState.Result(null, r.seconds, r.top, pcm, remote = true, gallery = r.gallery, threshold = r.threshold)
                } catch (e: Exception) { AppState.identify.value = IdentifyState.Failed("Laptop could not identify: ${e.message}") }
            }
            return
        }
        if (speechS < IDENTIFY_MIN_S) { AppState.identify.value = IdentifyState.Failed("Not enough speech heard (${"%.1f".format(speechS)} s). Hold the phone closer and try again."); return }
        val frameLen = req.frames.first().size
        val wav = FloatArray(req.frames.size * frameLen); var off = 0
        for (fr in req.frames) { System.arraycopy(fr, 0, wav, off, frameLen); off += frameLen }
        val pcm = ShortArray(wav.size) { (wav[it] * 32767f).toInt().coerceIn(-32767, 32767).toShort() }
        val emb = (embedder ?: return).embed(wav)
        AppState.identify.value = IdentifyState.Result(emb, speechS, g.topMatches(emb, 5), pcm)
    }

    private fun stopForegroundCompat() {
        if (Build.VERSION.SDK_INT >= 24) stopForeground(STOP_FOREGROUND_REMOVE) else @Suppress("DEPRECATION") stopForeground(true)
    }

    private fun stopListening() {
        running = false
        worker?.join(2000)
        worker = null
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        AppState.listening.value = false
        AppState.detection.value = null
        AppState.level.value = 0f
        ListenTile.refresh(this)
        stopForegroundCompat()
    }

    override fun onDestroy() { stopListening(); super.onDestroy() }
}
