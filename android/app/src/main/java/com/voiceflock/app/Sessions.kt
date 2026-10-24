package com.voiceflock.app

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** One named listening session: "Finance committee, room 215". Who spoke, how long, how often. */
data class Tally(val personId: Int, val name: String, var seconds: Float = 0f, var hits: Int = 0, var first: Long = 0L, var last: Long = 0L, var flagged: Boolean = false)

data class Session(val id: String, val name: String, val place: String, val startedAt: Long, var endedAt: Long? = null,
                   val record: Boolean = false, var audioPath: String? = null, var tally: List<Tally> = emptyList(), var sent: Boolean = false) {
    val label get() = if (place.isBlank()) name else "$name · $place"
    val durationS get() = (((endedAt ?: System.currentTimeMillis()) - startedAt) / 1000).toInt()
}

object Sessions {
    val active = MutableStateFlow<Session?>(null)
    val tally = MutableStateFlow<Map<Int, Tally>>(emptyMap())
    val history = MutableStateFlow<List<Session>>(emptyList())
    private fun file(ctx: Context) = File(ctx.filesDir, "sessions.json")

    fun load(ctx: Context) {
        val f = file(ctx); if (!f.exists()) return
        try {
            val j = JSONObject(f.readText())
            history.value = parseList(j.optJSONArray("history") ?: JSONArray())
            j.optJSONObject("active")?.let { a -> active.value = parse(a); tally.value = parse(a).tally.associateBy { it.personId }.toMutableMap() }
        } catch (_: Exception) {}
    }

    private fun parseList(a: JSONArray) = (0 until a.length()).map { parse(a.getJSONObject(it)) }
    private fun parse(o: JSONObject): Session {
        val t = o.optJSONArray("tally") ?: JSONArray()
        return Session(o.getString("id"), o.getString("name"), o.optString("place", ""), o.getLong("started"), o.optLong("ended", 0L).takeIf { it > 0 },
            o.optBoolean("record", false), o.optString("audio", "").ifEmpty { null },
            (0 until t.length()).map { i -> val x = t.getJSONObject(i); Tally(x.getInt("pid"), x.getString("name"), x.getDouble("s").toFloat(), x.getInt("hits"), x.optLong("first"), x.optLong("last"), x.optBoolean("flag")) },
            o.optBoolean("sent", false))
    }
    private fun json(s: Session) = JSONObject().put("id", s.id).put("name", s.name).put("place", s.place).put("started", s.startedAt).put("ended", s.endedAt ?: 0L)
        .put("record", s.record).put("audio", s.audioPath ?: "").put("sent", s.sent)
        .put("tally", JSONArray(s.tally.map { JSONObject().put("pid", it.personId).put("name", it.name).put("s", it.seconds.toDouble()).put("hits", it.hits).put("first", it.first).put("last", it.last).put("flag", it.flagged) }))

    @Synchronized
    private fun save(ctx: Context) {
        val a = active.value?.let { it.copy(tally = tally.value.values.toList()) }
        file(ctx).writeText(JSONObject().put("history", JSONArray(history.value.map(::json))).put("active", a?.let(::json) ?: JSONObject.NULL).toString())
    }

    fun start(ctx: Context, name: String, place: String, record: Boolean): Session {
        val s = Session(java.util.UUID.randomUUID().toString().take(8), name.trim().ifEmpty { "Session" }, place.trim(), System.currentTimeMillis(), record = record)
        if (record) s.audioPath = File(File(ctx.filesDir, "sessions").apply { mkdirs() }, "session-${s.id}.wav").path
        active.value = s; tally.value = emptyMap(); save(ctx)
        return s
    }

    /** A detection frame attributed to someone: add the hop to their speaking time. */
    fun noteSpeech(ctx: Context, personId: Int, name: String, seconds: Float, flagged: Boolean) {
        if (active.value == null) return
        val m = tally.value.toMutableMap()
        val t = m.getOrPut(personId) { Tally(personId, name, flagged = flagged) }
        t.seconds += seconds; t.flagged = t.flagged || flagged
        val now = System.currentTimeMillis(); if (t.first == 0L) t.first = now; t.last = now
        tally.value = m
    }

    fun noteHit(ctx: Context, personId: Int, name: String, at: Long, flagged: Boolean) {
        if (active.value == null) return
        val m = tally.value.toMutableMap()
        val t = m.getOrPut(personId) { Tally(personId, name, flagged = flagged) }
        t.hits += 1; if (t.first == 0L) t.first = at; t.last = at; t.flagged = t.flagged || flagged
        tally.value = m; save(ctx)
    }

    fun end(ctx: Context): Session? {
        val s = active.value ?: return null
        s.endedAt = System.currentTimeMillis()
        s.tally = tally.value.values.sortedByDescending { it.seconds }
        if (s.audioPath != null && !File(s.audioPath!!).exists()) s.audioPath = null
        history.value = listOf(s) + history.value.take(50)
        active.value = null; tally.value = emptyMap(); save(ctx)
        return s
    }

    fun update(ctx: Context, s: Session) { history.value = history.value.map { if (it.id == s.id) s else it }; save(ctx) }
    fun delete(ctx: Context, s: Session) { s.audioPath?.let { try { File(it).delete() } catch (_: Exception) {} }; history.value = history.value.filter { it.id != s.id }; save(ctx) }

    fun fmt(sec: Int): String = if (sec >= 3600) "%d:%02d:%02d".format(sec / 3600, sec % 3600 / 60, sec % 60) else "%d:%02d".format(sec / 60, sec % 60)
    private fun hm(at: Long) = SimpleDateFormat("HH:mm", Locale.US).format(Date(at))

    fun summaryText(s: Session): String {
        val sb = StringBuilder()
        sb.append("VoiceFlock session: ${s.label}\n")
        sb.append(SimpleDateFormat("EEE d MMM yyyy, HH:mm", Locale.US).format(Date(s.startedAt))).append(" · ").append(fmt(s.durationS)).append("\n\n")
        if (s.tally.isEmpty()) sb.append("No known voices heard.\n")
        for (t in s.tally) sb.append(if (t.flagged) "🔔 " else "• ").append(t.name).append(" · spoke ~").append(fmt(t.seconds.toInt())).append(" · heard ").append(t.hits).append("× · ").append(hm(t.first)).append("–").append(hm(t.last)).append("\n")
        return sb.toString()
    }

    fun csv(s: Session): String = "name,speaking_seconds,hits,first,last,flagged\n" + s.tally.joinToString("\n") {
        "\"${it.name.replace("\"", "'")}\",${it.seconds.toInt()},${it.hits},${hm(it.first)},${hm(it.last)},${if (it.flagged) 1 else 0}"
    }
}

/** Streams int16 PCM into a WAV file; the header is finalised on close. */
class WavWriter(path: String, private val sr: Int = 16000) {
    private val raf = java.io.RandomAccessFile(path, "rw").also { it.setLength(0); it.write(ByteArray(44)) }
    private var bytes = 0L
    fun write(pcm: ShortArray, n: Int) {
        val bb = java.nio.ByteBuffer.allocate(n * 2).order(java.nio.ByteOrder.LITTLE_ENDIAN); for (i in 0 until n) bb.putShort(pcm[i])
        raf.write(bb.array()); bytes += n * 2L
    }
    fun close() {
        val h = java.nio.ByteBuffer.allocate(44).order(java.nio.ByteOrder.LITTLE_ENDIAN)
        h.put("RIFF".toByteArray()).putInt((36 + bytes).toInt()).put("WAVE".toByteArray()).put("fmt ".toByteArray()).putInt(16).putShort(1).putShort(1).putInt(sr).putInt(sr * 2).putShort(2).putShort(16).put("data".toByteArray()).putInt(bytes.toInt())
        raf.seek(0); raf.write(h.array()); raf.close()
    }
}
