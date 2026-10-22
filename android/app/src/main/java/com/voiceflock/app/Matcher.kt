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
 * Same logic as the laptop's Live mode: a rolling window embedded every hop, scored against the gallery,
 * smoothed over consecutive speech windows; silence resets the smoothing.
 */
class Matcher(
    private val embedder: Embedder,
    private val gallery: Gallery,
    private val isFlagged: (Person) -> Boolean,
    private val threshold: Float,
    windowS: Float = gallery.windowS,
    hopS: Float = gallery.hopS,
    private val minLevelDb: Float = gallery.minLevelDb,
    private val smooth: Int = 3,
) {
    private val sr = 16000
    private val window = (windowS * sr).toInt()
    private val hop = (hopS * sr).toInt()
    private val buf = FloatArray(window)
    private var filled = 0
    private var sinceHop = 0
    private var seen = 0L
    private val history = ArrayDeque<Map<Int, Float>>()

    fun feed(pcm: ShortArray, n: Int): List<Detection> {
        val out = ArrayList<Detection>(1)
        for (i in 0 until n) {
            val v = pcm[i] / 32768f
            if (filled < window) { buf[filled++] = v } else { System.arraycopy(buf, 1, buf, 0, window - 1); buf[window - 1] = v }
            seen++
            sinceHop++
            if (sinceHop >= hop && filled >= minOf(window, sr)) {
                sinceHop = 0
                out.add(analyse())
            }
        }
        return out
    }

    private fun analyse(): Detection {
        val t = seen / sr.toFloat()
        var acc = 0.0
        for (i in 0 until filled) acc += (buf[i] * buf[i]).toDouble()
        val rms = sqrt(acc / filled + 1e-12)
        val level = (20 * log10(rms + 1e-9)).toFloat()
        if (level < minLevelDb) {
            history.clear()
            return Detection(t, level, false, emptyList(), false)
        }
        val emb = embedder.embed(if (filled == window) buf else buf.copyOf(filled))
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
        return Detection(t, level, true, ranked, alert)
    }
}
