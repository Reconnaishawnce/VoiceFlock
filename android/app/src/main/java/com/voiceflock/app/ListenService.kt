package com.voiceflock.app

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
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.concurrent.thread

/** Foreground service: microphone -> Matcher -> alerts. Runs with the screen off. */
class ListenService : Service() {
    companion object {
        const val ACTION_START = "com.voiceflock.app.START"
        const val ACTION_STOP = "com.voiceflock.app.STOP"
        private const val SR = 16000

        fun start(context: Context) {
            val i = Intent(context, ListenService::class.java).setAction(ACTION_START)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(i) else context.startService(i)
        }
        fun stop(context: Context) { context.startService(Intent(context, ListenService::class.java).setAction(ACTION_STOP)) }
    }

    @Volatile private var running = false
    private var worker: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val lastAlertAt = HashMap<Int, Long>()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) { stopListening(); stopSelf(); return START_NOT_STICKY }
        if (running) return START_STICKY
        Alerts.ensureChannels(this)
        Alerts.initTts(this)
        val notif = Alerts.serviceNotification(this, "starting…")
        if (Build.VERSION.SDK_INT >= 29) startForeground(Alerts.NOTIF_SERVICE, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        else startForeground(Alerts.NOTIF_SERVICE, notif)
        startListening()
        return START_STICKY
    }

    private fun startListening() {
        val prefs = Prefs(this)
        val gallery = Gallery.load(this)
        val model = Gallery.modelFile(this)
        if (gallery == null || !model.exists()) {
            AppState.status.value = "Sync with the laptop first (Settings)."
            stopSelf(); return
        }
        AppState.gallery.value = gallery
        val threshold = if (prefs.thresholdOverride > 0f) prefs.thresholdOverride else gallery.threshold
        val flagged: (Person) -> Boolean = { p -> prefs.isFlagged(p, gallery) }
        running = true
        AppState.listening.value = true
        AppState.status.value = "loading model…"
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "voiceflock:listen").also { it.acquire() }

        worker = thread(name = "voiceflock-listen") {
            var embedder: Embedder? = null
            var record: AudioRecord? = null
            try {
                embedder = Embedder(model.path)
                val matcher = Matcher(embedder, gallery, flagged, threshold)
                val minBuf = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
                record = AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SR, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, SR))
                if (record.state != AudioRecord.STATE_INITIALIZED) throw IllegalStateException("microphone unavailable")
                record.startRecording()
                val nFlag = gallery.people.count(flagged)
                AppState.status.value = "listening · ${gallery.people.size} voices, $nFlag flagged"
                getSystemService(android.app.NotificationManager::class.java)
                    .notify(Alerts.NOTIF_SERVICE, Alerts.serviceNotification(this, "$nFlag flagged voice(s) · ${gallery.people.size} in library"))
                val pcm = ShortArray(SR / 10)
                val fmt = SimpleDateFormat("HH:mm:ss", Locale.US)
                while (running) {
                    val n = record.read(pcm, 0, pcm.size)
                    if (n <= 0) continue
                    for (d in matcher.feed(pcm, n)) {
                        AppState.detection.value = d
                        if (d.alert) {
                            val top = d.top.first()
                            val now = System.currentTimeMillis()
                            val last = lastAlertAt[top.person.id] ?: 0L
                            if (now - last > prefs.cooldownSeconds * 1000L) {
                                lastAlertAt[top.person.id] = now
                                Alerts.voiceAlert(this, top.person.name, top.probability)
                                Alerts.performAlert(this, top.person.name, prefs.soundFor(top.person, gallery),
                                    prefs.messageFor(top.person, gallery), prefs.demoMode)
                                AppState.pushLog("${fmt.format(Date())}  ${top.person.name}  ${(top.probability * 100).toInt()}%")
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                AppState.status.value = "error: ${e.message}"
                AppState.pushLog("error: ${e.message}")
            } finally {
                try { record?.stop() } catch (_: Exception) {}
                record?.release()
                embedder?.close()
                AppState.listening.value = false
                if (!AppState.status.value.startsWith("error")) AppState.status.value = "stopped"
            }
        }
    }

    private fun stopListening() {
        running = false
        worker?.join(2000)
        worker = null
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        AppState.listening.value = false
        AppState.detection.value = null
        if (Build.VERSION.SDK_INT >= 24) stopForeground(STOP_FOREGROUND_REMOVE) else @Suppress("DEPRECATION") stopForeground(true)
    }

    override fun onDestroy() { stopListening(); super.onDestroy() }
}
