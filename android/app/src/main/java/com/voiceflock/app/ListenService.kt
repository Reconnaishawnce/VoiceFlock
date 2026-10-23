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
import kotlin.concurrent.thread
import kotlin.math.log10
import kotlin.math.sqrt

/** What the Identify sheet shows. */
sealed class IdentifyState {
    data class Listening(val speechS: Float, val elapsedS: Float) : IdentifyState()
    data class Result(val embedding: FloatArray, val speechS: Float, val top: List<Match>) : IdentifyState()
    data class Failed(val message: String) : IdentifyState()
}

/** Foreground service: microphone -> Matcher -> sightings log + alerts. Runs with the screen off. */
class ListenService : Service() {
    companion object {
        const val ACTION_START = "com.voiceflock.app.START"
        const val ACTION_STOP = "com.voiceflock.app.STOP"
        const val ACTION_IDENTIFY = "com.voiceflock.app.IDENTIFY"
        const val ACTION_IDENTIFY_CANCEL = "com.voiceflock.app.IDENTIFY_CANCEL"
        private const val SR = 16000
        private const val IDENTIFY_SPEECH_S = 6f      // stop collecting after this much speech
        private const val IDENTIFY_MAX_S = 15f        // or this much wall time
        private const val IDENTIFY_MIN_S = 1.5f       // below this there is nothing to identify

        fun start(context: Context) = send(context, ACTION_START)
        fun stop(context: Context) { context.startService(Intent(context, ListenService::class.java).setAction(ACTION_STOP)) }
        /** One-shot identification. Starts the service for the duration if it is not already listening. */
        fun identify(context: Context) = send(context, ACTION_IDENTIFY)
        fun cancelIdentify(context: Context) { context.startService(Intent(context, ListenService::class.java).setAction(ACTION_IDENTIFY_CANCEL)) }
        private fun send(context: Context, action: String) {
            val i = Intent(context, ListenService::class.java).setAction(action)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(i) else context.startService(i)
        }
    }

    private class IdentifyReq(val startedAt: Long = System.currentTimeMillis()) { val frames = ArrayList<FloatArray>() }

    @Volatile private var running = false
    @Volatile private var oneShot = false
    @Volatile private var identifyReq: IdentifyReq? = null
    private var worker: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val lastSeenAt = HashMap<Int, Long>()

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
            if (wantIdentify) { identifyReq = IdentifyReq(); AppState.identify.value = IdentifyState.Listening(0f, 0f) }
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

    private fun startListening() {
        val prefs = Prefs(this)
        var gallery = Gallery.load(this)
        val model = Gallery.modelFile(this)
        if (gallery == null || !model.exists()) {
            val msg = if (gallery == null) "No voice library yet: import one in Settings." else "Speaker model not downloaded yet (Settings)."
            AppState.status.value = msg
            if (identifyReq != null) AppState.identify.value = IdentifyState.Failed(msg)
            identifyReq = null
            stopForegroundCompat(); stopSelf(); return
        }
        AppState.gallery.value = gallery
        running = true
        AppState.listening.value = true
        AppState.listeningSince.value = System.currentTimeMillis()
        AppState.speechSeconds.value = 0f
        AppState.status.value = "loading model…"
        ListenTile.refresh(this)
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "voiceflock:listen").also { it.acquire() }
        val nm = getSystemService(NotificationManager::class.java)

        worker = thread(name = "voiceflock-listen") {
            var embedder: Embedder? = null
            var record: AudioRecord? = null
            try {
                embedder = Embedder(model.path)
                var g = gallery!!
                fun threshold() = if (prefs.thresholdOverride > 0f) prefs.thresholdOverride else g.threshold
                fun newMatcher(): Matcher = Matcher(embedder, g, { p -> prefs.isFlagged(p, g) }, threshold()).also { m ->
                    m.onSpeechFrame = { fr -> identifyReq?.frames?.add(fr) }
                }
                var matcher = newMatcher()
                fun describe(): String { val nFlag = g.people.count { prefs.isFlagged(it, g) }; return "$nFlag flagged · ${g.people.size} voices" }
                val minBuf = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
                record = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SR, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, SR))
                if (record.state != AudioRecord.STATE_INITIALIZED) throw IllegalStateException("microphone unavailable (another app holds it?)")
                record.startRecording()
                AppState.status.value = "listening · ${describe()}"
                nm.notify(Alerts.NOTIF_SERVICE, Alerts.serviceNotification(this, describe()))
                val pcm = ShortArray(SR / 10)
                var lastNotif = 0L; var notifText = ""
                var lastSync = 0L
                val syncing = java.util.concurrent.atomic.AtomicBoolean(false)
                while (running) {
                    val n = record.read(pcm, 0, pcm.size)
                    if (n <= 0) continue
                    // level for the ring: dBFS over the last 100 ms, mapped -60..-10 dB -> 0..1
                    var acc = 0.0; for (i in 0 until n) { val v = pcm[i] / 32768.0; acc += v * v }
                    val db = 20 * log10(sqrt(acc / n) + 1e-9)
                    AppState.level.value = ((db + 60) / 50).toFloat().coerceIn(0f, 1f)
                    // the library changed under us (Identify added a print, a sync arrived): rebuild cheaply, keep the model
                    val cur = AppState.gallery.value
                    if (cur != null && cur !== g) { g = cur; matcher = newMatcher(); AppState.status.value = "listening · ${describe()}"; nm.notify(Alerts.NOTIF_SERVICE, Alerts.serviceNotification(this, describe())) }
                    for (d in matcher.feed(pcm, n)) {
                        AppState.detection.value = d
                        AppState.speechSeconds.value = matcher.speechSeconds
                        val now = System.currentTimeMillis()
                        val top = d.top.firstOrNull()
                        val text = if (d.speech && top != null) "hearing ${top.person.name} · ${(top.probability * 100).toInt()}%" else describe()
                        if (text != notifText && now - lastNotif > 3000) { notifText = text; lastNotif = now; nm.notify(Alerts.NOTIF_SERVICE, Alerts.serviceNotification(this, text)) }
                        // while identifying on demand (service started only for that) stay quiet: no log entries, no alarms
                        if (!oneShot && top != null && d.speech && top.probability >= threshold() && (top.person.kind == "named" || top.flagged)) {
                            val last = lastSeenAt[top.person.id] ?: 0L
                            if (now - last > prefs.cooldownSeconds * 1000L) {
                                lastSeenAt[top.person.id] = now
                                Sightings.add(this, Sighting(now, top.person.id, top.person.name, top.probability, d.alert))
                                if (d.alert) {
                                    Alerts.voiceAlert(this, top.person, top.probability)
                                    Alerts.performAlert(this, top.person.name, prefs.soundFor(top.person, g), prefs.messageFor(top.person, g), prefs.demoMode)
                                }
                            }
                        }
                    }
                    identifyReq?.let { req -> stepIdentify(req, embedder, g) }
                    // every 30 s: this phone's sightings up to the laptop, the laptop's down to the phone (notify on flagged ones)
                    val tnow = System.currentTimeMillis()
                    if (!oneShot && prefs.autoSync && Sync.connected(prefs) && tnow - lastSync > 30_000 && syncing.compareAndSet(false, true)) {
                        lastSync = tnow
                        thread(name = "voiceflock-sync") {
                            try {
                                kotlinx.coroutines.runBlocking {
                                    try { Sync.pushSightings(this@ListenService, prefs) } catch (_: Exception) {}
                                    val fresh = try { Sync.pullSightings(this@ListenService, prefs) } catch (_: Exception) { emptyList() }
                                    for (s in fresh.filter { it.flagged && it.at > tnow - 10 * 60_000 }) Alerts.remoteNotification(this@ListenService, s)
                                    if (tnow - prefs.lastSyncMs > 5 * 60_000) try { Sync.pullGallery(this@ListenService, prefs)?.let { reloadGallery(this@ListenService) }; prefs.lastSyncMs = tnow } catch (_: Exception) {}
                                }
                            } finally { syncing.set(false) }
                        }
                    }
                    if (oneShot && identifyReq == null) running = false
                }
            } catch (e: Exception) {
                AppState.status.value = "error: ${e.message}"
                identifyReq?.let { AppState.identify.value = IdentifyState.Failed(e.message ?: "error"); identifyReq = null }
            } finally {
                try { record?.stop() } catch (_: Exception) {}
                record?.release()
                embedder?.close()
                AppState.listening.value = false
                AppState.level.value = 0f
                if (!AppState.status.value.startsWith("error")) AppState.status.value = "stopped"
                ListenTile.refresh(this)
                if (oneShot) { AppState.detection.value = null; wakeLock?.let { if (it.isHeld) it.release() }; wakeLock = null; stopForegroundCompat(); stopSelf() }
            }
        }
    }

    /** Collect speech frames until there is enough, then score the whole clip against the library. */
    private fun stepIdentify(req: IdentifyReq, embedder: Embedder, g: Gallery) {
        val speechS = req.frames.size * 0.1f
        val elapsed = (System.currentTimeMillis() - req.startedAt) / 1000f
        if (speechS < IDENTIFY_SPEECH_S && elapsed < IDENTIFY_MAX_S) { AppState.identify.value = IdentifyState.Listening(speechS, elapsed); return }
        identifyReq = null
        if (speechS < IDENTIFY_MIN_S) { AppState.identify.value = IdentifyState.Failed("Not enough speech heard (${"%.1f".format(speechS)} s). Hold the phone closer and try again."); return }
        val frameLen = req.frames.first().size
        val wav = FloatArray(req.frames.size * frameLen); var off = 0
        for (fr in req.frames) { System.arraycopy(fr, 0, wav, off, frameLen); off += frameLen }
        val emb = embedder.embed(wav)
        AppState.identify.value = IdentifyState.Result(emb, speechS, g.topMatches(emb, 5))
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
