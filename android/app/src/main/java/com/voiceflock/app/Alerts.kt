package com.voiceflock.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import androidx.core.app.NotificationCompat
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import kotlin.concurrent.thread
import kotlin.math.PI
import kotlin.math.sin

object Alerts {
    const val CHANNEL_SERVICE = "listening"
    const val CHANNEL_ALERT = "voice_alert"
    const val NOTIF_SERVICE = 1
    const val NOTIF_ALERT = 2

    fun ensureChannels(context: Context) {
        val nm = context.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL_SERVICE, "Listening", NotificationManager.IMPORTANCE_LOW).apply {
            description = "Shown while VoiceFlock is listening"
        })
        if (nm.getNotificationChannel(CHANNEL_ALERT) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL_ALERT, "Voice alerts", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "A flagged voice was heard"
                enableVibration(true)
                vibrationPattern = longArrayOf(0, 250, 150, 250)
                setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION),
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT).build())
            })
        }
    }

    fun serviceNotification(context: Context, text: String): android.app.Notification {
        val open = PendingIntent.getActivity(context, 0, Intent(context, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(context, 1, Intent(context, ListenService::class.java).setAction(ListenService.ACTION_STOP), PendingIntent.FLAG_IMMUTABLE)
        return NotificationCompat.Builder(context, CHANNEL_SERVICE)
            .setSmallIcon(R.drawable.ic_stat_wave)
            .setContentTitle("VoiceFlock is listening")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(android.R.drawable.ic_media_pause, "Stop", stop)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    // ------------------------------------------------------------------ the bit: alarm, then speech
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    @Volatile private var playing = false

    fun initTts(context: Context) {
        if (tts != null) return
        tts = TextToSpeech(context.applicationContext) { status ->
            ttsReady = status == TextToSpeech.SUCCESS
            if (ttsReady) {
                tts?.language = java.util.Locale.US
                tts?.setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            }
        }
    }

    /** Synthesised alarm, written straight to an AudioTrack on the ALARM stream (ignores media volume / silent). */
    private fun synth(kind: String): ShortArray {
        val sr = 22050
        return when (kind) {
            "none" -> ShortArray(0)
            "horn" -> ShortArray((sr * 5.8).toInt()).also { buf ->   // two-tone emergency horn, 0.7 s per tone, 8 tones
                var ph = 0.0
                for (i in buf.indices) { val t = i.toDouble() / sr; val k = (t / 0.7).toInt(); val tt = t - k * 0.7
                    val f = if (k % 2 == 0) 330.0 else 415.0; ph += 2 * PI * f / sr
                    val env = if (tt < 0.05) tt / 0.05 else if (tt > 0.6) ((0.66 - tt) / 0.06).coerceIn(0.0, 1.0) else 1.0
                    val v = (sin(ph) + 0.6 * sin(2 * ph) + 0.4 * sin(3 * ph) + 0.3 * (if (sin(ph / 2) >= 0) 1.0 else -1.0)) / 2.3
                    buf[i] = (v * env * 0.9 * 32767).toInt().coerceIn(-32767, 32767).toShort() }
            }
            "vault" -> ShortArray(sr * 7).also { buf ->              // blast door: sub thump, clank, grinding sweep, hiss
                val r = java.util.Random(7); var ph = 0.0; var subPh = 0.0; var lfo = 0.0
                var ring1 = 0.0; var ring2 = 0.0; var lpState = 0.0
                for (i in buf.indices) { val t = i.toDouble() / sr; var v = 0.0
                    if (t < 1.5) { val f = 60 * Math.pow(0.5, t / 1.2); subPh += 2 * PI * f / sr; v += sin(subPh) * Math.exp(-t * 3.0) * 1.0 }
                    if (t in 0.05..0.95) { val n = r.nextGaussian() * 0.6; ring1 = ring1 * 0.995 + n * 0.02; ring2 = ring2 * 0.992 + n * 0.015
                        v += (ring1 * sin(2 * PI * 1900 * t) + ring2 * sin(2 * PI * 2600 * t)) * 8 * Math.exp(-(t - 0.05) * 4) }
                    if (t in 0.8..6.1) { val tt = t - 0.8; val f = 70 + 80 * (tt / 5.0).coerceAtMost(1.0); ph += 2 * PI * f / sr; lfo += 2 * PI * (9 + 9 * tt / 5.0) / sr
                        val saw = ((ph / (2 * PI)) % 1.0) * 2 - 1; val cutoff = 0.02 + 0.08 * (tt / 5.0); lpState += cutoff * (saw - lpState)
                        val env = (if (tt < 0.8) tt / 0.8 else if (tt > 4.4) ((5.3 - tt) / 0.9).coerceIn(0.0, 1.0) else 1.0)
                        v += lpState * env * 0.55 * (1 + 0.3 * sin(lfo)) }
                    if (t > 5.6) { v += r.nextGaussian() * 0.12 * Math.exp(-(t - 5.6) * 2.5) }
                    buf[i] = (v * 32767).toInt().coerceIn(-32767, 32767).toShort() }
            }
            "radar" -> ShortArray((sr * 5.2).toInt()).also { buf ->  // phone-style alarm: 4 pips, pause, ×4
                var ph = 0.0
                for (i in buf.indices) { val t = i.toDouble() / sr; val rep = (t / 1.3).toInt(); val tt = t - rep * 1.3; val pip = (tt / 0.16).toInt()
                    val on = rep < 4 && pip < 4 && (tt - pip * 0.16) < 0.09
                    val f = if (pip % 2 == 0) 1000.0 else 1250.0; ph += 2 * PI * f / sr
                    buf[i] = (if (on) (if (sin(ph) >= 0) 0.5 else -0.5) * 32767 * 0.5 else 0.0).toInt().toShort() }
            }
            "klaxon" -> ShortArray(sr * 4).also { buf ->   // pulsed 330 Hz buzzer, 0.3 s on / 0.2 s off
                var ph = 0.0
                for (i in buf.indices) { val on = (i % (sr / 2)) < sr * 3 / 10; ph += 2 * PI * 330 / sr
                    buf[i] = (if (on) (if (sin(ph) >= 0) 0.6 else -0.6) * 32767 * 0.5 else 0.0).toInt().toShort() }
            }
            "chime" -> ShortArray((sr * 1.8).toInt()).also { buf ->
                val notes = doubleArrayOf(660.0, 880.0, 1320.0)
                for (i in buf.indices) { val t = i.toDouble() / sr; var v = 0.0
                    for ((k, f) in notes.withIndex()) { val st = k * 0.18; if (t >= st) v += 0.3 * sin(2 * PI * f * (t - st)) * Math.exp(-(t - st) * 3.0) }
                    buf[i] = (v * 32767).toInt().coerceIn(-32767, 32767).toShort() }
            }
            else -> ShortArray(sr * 8).also { buf ->      // siren: 400->800->400 Hz over 4 s, twice, with a 2nd harmonic, soft edges
                var ph = 0.0; var ph2 = 0.0
                for (i in buf.indices) { val t = i.toDouble() / sr; val c = t % 4.0
                    val f = if (c < 2) 400 * Math.pow(2.0, c / 2) else 800 * Math.pow(0.5, (c - 2) / 2)
                    ph += 2 * PI * f / sr; ph2 += 2 * PI * 2 * f / sr
                    val env = minOf(1.0, t / 0.6, (8.0 - t) / 0.8).coerceIn(0.0, 1.0)
                    // sawtooth-ish via a few harmonics keeps it loud but not piercing
                    val v = (sin(ph) + 0.5 * sin(2 * ph) + 0.25 * sin(3 * ph) + 0.3 * sin(ph2)) / 2.05
                    buf[i] = (v * env * 0.9 * 32767).toInt().coerceIn(-32767, 32767).toShort() }
            }
        }
    }

    private fun playTrack(pcm: ShortArray) {
        if (pcm.isEmpty()) return
        val sr = 22050
        val track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM).setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(sr).setEncoding(AudioFormat.ENCODING_PCM_16BIT).setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setBufferSizeInBytes(pcm.size * 2).setTransferMode(AudioTrack.MODE_STATIC).build()
        track.write(pcm, 0, pcm.size)
        track.play()
        Thread.sleep((pcm.size * 1000L / sr) + 100)
        track.stop(); track.release()
    }

    /**
     * Alarm sound followed by the spoken line. With demoMode the ALARM stream is pushed to max for the
     * duration and restored afterwards. Runs on its own thread; overlapping calls are dropped.
     */
    fun performAlert(context: Context, name: String, sound: String, message: String, demoMode: Boolean) {
        if (playing) return
        playing = true
        initTts(context)
        thread(name = "voiceflock-alert") {
            val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
            val old = am.getStreamVolume(AudioManager.STREAM_ALARM)
            try {
                if (demoMode) try { am.setStreamVolume(AudioManager.STREAM_ALARM, am.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0) } catch (_: Exception) {}
                playTrack(synth(sound))
                val t = tts
                if (t != null && ttsReady && message.isNotBlank()) {
                    val done = java.util.concurrent.CountDownLatch(1)
                    t.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                        override fun onStart(id: String?) {}
                        override fun onDone(id: String?) { done.countDown() }
                        @Deprecated("Deprecated in Java") override fun onError(id: String?) { done.countDown() }
                    })
                    t.setSpeechRate(0.95f)
                    t.speak(message, TextToSpeech.QUEUE_FLUSH, null, "vf-${System.currentTimeMillis()}")
                    done.await(20, java.util.concurrent.TimeUnit.SECONDS)
                }
            } catch (_: Exception) {
            } finally {
                if (demoMode) try { am.setStreamVolume(AudioManager.STREAM_ALARM, old, 0) } catch (_: Exception) {}
                playing = false
            }
        }
    }

    /** One notification per person (so two flagged voices in a row both stay visible). */
    fun voiceAlert(context: Context, person: Person, probability: Float) {
        val nm = context.getSystemService(NotificationManager::class.java)
        val open = PendingIntent.getActivity(context, 0, Intent(context, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val n = NotificationCompat.Builder(context, CHANNEL_ALERT)
            .setSmallIcon(R.drawable.ic_stat_wave)
            .setColor(0xFFE8B35A.toInt())
            .setContentTitle("Voice heard: ${person.name}")
            .setContentText("${(probability * 100).toInt()}% match · ${java.text.SimpleDateFormat("HH:mm", java.util.Locale.US).format(java.util.Date())}")
            .setContentIntent(open)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .build()
        nm.notify(NOTIF_ALERT + Math.floorMod(person.id, 100_000), n)
        vibrate(context)
    }

    /** A flagged voice was heard by the laptop (Live mode or a new recording): notify, no alarm. */
    fun remoteNotification(context: Context, s: Sighting) {
        val nm = context.getSystemService(NotificationManager::class.java)
        val open = PendingIntent.getActivity(context, 0, Intent(context, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val where = if (s.device == "laptop") "on the laptop (Live)" else "in a new recording" + (s.source?.let { ": $it" } ?: "")
        val n = NotificationCompat.Builder(context, CHANNEL_ALERT)
            .setSmallIcon(R.drawable.ic_stat_wave).setColor(0xFF7FB5FF.toInt())
            .setContentTitle("${s.name} heard $where")
            .setContentText("${(s.probability * 100).toInt()}% · ${java.text.SimpleDateFormat("HH:mm", java.util.Locale.US).format(java.util.Date(s.at))}")
            .setContentIntent(open).setAutoCancel(true).setPriority(NotificationCompat.PRIORITY_HIGH).build()
        nm.notify(50_000 + Math.floorMod((s.at / 1000).toInt() + s.name.hashCode(), 40_000), n)
    }

    private fun vibrate(context: Context) {
        val v: Vibrator = if (Build.VERSION.SDK_INT >= 31)
            (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager).defaultVibrator
        else @Suppress("DEPRECATION") context.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
        v.vibrate(VibrationEffect.createWaveform(longArrayOf(0, 250, 150, 250), -1))
    }
}
