package com.voiceflock.app

import kotlin.math.log10
import kotlin.math.sqrt

data class Detection(
    val tSeconds: Float,
    val levelDb: Float,
    val speech: Boolean,
    val top: List<Scored>,
    val alert: Boolean,
)

data class Scored(val person: Person, val probability: Float, val instant: Float, val flagged: Boolean)

/**
 * Same logic as the laptop's Live mode: keep only speech frames (100 ms frames above an adaptive
 * noise floor), embed the last `windowS` seconds of speech every hop, smooth over consecutive windows;
 * a pause longer than 2 s clears the buffer so the next speaker starts fresh.
 */
class Matcher(
    private val embedder: Embedder,
    private val gallery: Gallery,
    private val isFlagged: (Person) -> Boolean,
    private val threshold: Float,
    private val windowS: Float = gallery.windowS,
    hopS: Float = gallery.hopS,
    private val minLevelDb: Float = gallery.minLevelDb,
    private val smooth: Int = 3,
) {
    private val sr = 16000
    private val frameS = 0.1f
    private val frameLen = (frameS * sr).toInt()
    private val hop = (hopS * sr).toInt()
    private val minSpeechS = 1.0f
    private val pauseResetS = 2.0f
    private val floorMarginDb = 8f
    private val pending = FloatArray(frameLen)
    private var pendingFill = 0
    private val levels = ArrayDeque<Float>()
    private val speech = ArrayDeque<FloatArray>()
    private var sinceHop = 0
    private var sinceSpeech = 0f
    private var seen = 0L
    private var lastLevel = -100f
    private val history = ArrayDeque<Map<Int, Float>>()
    /** Called with every 100 ms frame judged to be speech (used by Identify to collect a clip). */
    var onSpeechFrame: ((FloatArray) -> Unit)? = null
    var speechSeconds = 0f; private set

    private fun thresholdDb(): Float {
        if (levels.size < 10) return minLevelDb
        val sorted = levels.sorted()
        val floor = sorted[(sorted.size * 0.2f).toInt().coerceIn(0, sorted.size - 1)]
        return maxOf(minLevelDb, floor + floorMarginDb)
    }

    fun feed(pcm: ShortArray, n: Int): List<Detection> {
        val out = ArrayList<Detection>(1)
        for (i in 0 until n) {
            pending[pendingFill++] = pcm[i] / 32768f
            if (pendingFill < frameLen) continue
            pendingFill = 0
            val fr = pending.copyOf()
            seen += frameLen
            sinceHop += frameLen
            var acc = 0.0
            for (v in fr) acc += (v * v).toDouble()
            val level = (20 * log10(sqrt(acc / frameLen + 1e-12) + 1e-9)).toFloat()
            lastLevel = level
            val thr = thresholdDb()
            levels.addLast(level); while (levels.size > 100) levels.removeFirst()
            if (level > thr) {
                speech.addLast(fr); sinceSpeech = 0f; speechSeconds += frameS; onSpeechFrame?.invoke(fr)
                while (speech.size * frameS > windowS) speech.removeFirst()
            } else {
                sinceSpeech += frameS
                if (sinceSpeech >= pauseResetS && speech.isNotEmpty()) { speech.clear(); history.clear() }
            }
            if (sinceHop >= hop) { sinceHop = 0; out.add(analyse()) }
        }
        return out
    }

    private fun analyse(): Detection {
        val t = seen / sr.toFloat()
        val speechS = speech.size * frameS
        if (speechS < minSpeechS || sinceSpeech >= pauseResetS) {
            if (sinceSpeech >= pauseResetS) history.clear()
            return Detection(t, lastLevel, false, emptyList(), false)
        }
        val wav = FloatArray(speech.size * frameLen)
        var off = 0
        for (fr in speech) { System.arraycopy(fr, 0, wav, off, frameLen); off += frameLen }
        val emb = embedder.embed(wav)
        val matches = gallery.topMatches(emb, 3)
        history.addLast(matches.associate { it.person.id to it.probability })
        while (history.size > smooth) history.removeFirst()
        val merged = HashMap<Int, Float>()
        for (h in history) for ((pid, p) in h) merged[pid] = (merged[pid] ?: 0f) + p / history.size
        val byId = matches.associateBy { it.person.id }
        val people = gallery.people.associateBy { it.id }
        val ranked = merged.entries.sortedByDescending { it.value }.take(3).mapNotNull { (pid, p) ->
            val person = people[pid] ?: return@mapNotNull null
            Scored(person, p, byId[pid]?.probability ?: 0f, isFlagged(person))
        }
        val top = ranked.firstOrNull()
        val alert = top != null && top.flagged && top.probability >= threshold
        return Detection(t, lastLevel, true, ranked, alert)
    }
}
