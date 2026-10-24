package com.voiceflock.app

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

/** One "heard this voice" event. Kept on disk (JSON lines) so the log survives restarts. */
data class Sighting(val at: Long, val personId: Int, val name: String, val probability: Float, val flagged: Boolean,
                    val device: String = "phone", val source: String? = null, val clip: String? = null, val session: String? = null)

object Sightings {
    private const val MAX = 600
    private fun file(ctx: Context) = File(ctx.filesDir, "sightings.jsonl")

    fun load(ctx: Context): List<Sighting> {
        val f = file(ctx)
        if (!f.exists()) return emptyList()
        return f.readLines().mapNotNull { line ->
            try { val j = JSONObject(line); Sighting(j.getLong("at"), j.getInt("pid"), j.getString("name"), j.getDouble("p").toFloat(), j.optBoolean("flag", false), j.optString("dev", "phone"), j.optString("src", "").ifEmpty { null }, j.optString("clip", "").ifEmpty { null }, j.optString("ses", "").ifEmpty { null }) } catch (_: Exception) { null }
        }.asReversed().take(MAX)
    }

    private fun json(s: Sighting) = JSONObject().put("at", s.at).put("pid", s.personId).put("name", s.name).put("p", s.probability.toDouble()).put("flag", s.flagged).put("dev", s.device).put("src", s.source ?: "").put("clip", s.clip ?: "").put("ses", s.session ?: "")

    /** Forget a kept clip (file + reference) once it was sent or is not wanted. */
    @Synchronized
    fun dropClip(ctx: Context, s: Sighting) {
        s.clip?.let { try { File(it).delete() } catch (_: Exception) {} }
        AppState.sightings.value = AppState.sightings.value.map { if (it.at == s.at && it.name == s.name && it.device == s.device) it.copy(clip = null) else it }
        compact(ctx)
    }

    @Synchronized
    fun add(ctx: Context, s: Sighting) {
        file(ctx).appendText(json(s).toString() + "\n")
        AppState.sightings.value = (listOf(s) + AppState.sightings.value).sortedByDescending { it.at }.take(MAX)
        if (file(ctx).length() > 400_000) compact(ctx)
    }

    /** Sightings that happened elsewhere (laptop Live mode, new recordings), merged in without duplicates. */
    @Synchronized
    fun addRemote(ctx: Context, items: List<Sighting>): List<Sighting> {
        val have = AppState.sightings.value.map { Triple(it.at, it.name, it.device) }.toHashSet()
        val fresh = items.filter { Triple(it.at, it.name, it.device) !in have }
        if (fresh.isEmpty()) return fresh
        file(ctx).appendText(fresh.joinToString("") { json(it).toString() + "\n" })
        AppState.sightings.value = (fresh + AppState.sightings.value).sortedByDescending { it.at }.take(MAX)
        if (file(ctx).length() > 400_000) compact(ctx)
        return fresh
    }

    private fun compact(ctx: Context) {
        val keep = AppState.sightings.value.asReversed()
        file(ctx).writeText(keep.joinToString("") { s -> json(s).toString() + "\n" })
    }

    fun clear(ctx: Context) { file(ctx).delete(); AppState.sightings.value = emptyList() }

    fun csv(list: List<Sighting>): String = "time,name,probability,alerted,where\n" + list.asReversed().joinToString("\n") {
        "${SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date(it.at))},\"${it.name.replace("\"", "'")}\",${"%.3f".format(Locale.US, it.probability)},${if (it.flagged) 1 else 0},${it.device}${it.source?.let { s -> " (" + s.replace(",", " ") + ")" } ?: ""}"
    }

    fun time(at: Long): String = SimpleDateFormat("HH:mm", Locale.US).format(Date(at))

    fun dayLabel(at: Long): String {
        val c = Calendar.getInstance(); val d = Calendar.getInstance().apply { timeInMillis = at }
        val sameDay = { a: Calendar, b: Calendar -> a.get(Calendar.YEAR) == b.get(Calendar.YEAR) && a.get(Calendar.DAY_OF_YEAR) == b.get(Calendar.DAY_OF_YEAR) }
        if (sameDay(c, d)) return "Today"
        c.add(Calendar.DAY_OF_YEAR, -1)
        if (sameDay(c, d)) return "Yesterday"
        return SimpleDateFormat("EEE d MMM", Locale.US).format(Date(at))
    }
}
