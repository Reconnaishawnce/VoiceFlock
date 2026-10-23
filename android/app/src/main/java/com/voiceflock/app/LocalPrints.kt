package com.voiceflock.app

import android.content.Context
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** A voice captured on this phone with Identify: extra prints for a library person, or a brand-new person (negative id). */
data class LocalVoice(val id: Int, val personId: Int?, val name: String, val prints: List<FloatArray>, val createdAt: Long)

/**
 * Voiceprints made on the phone live in phone_prints.json and are merged into the library at load time.
 * They travel back to the laptop inside "Share my edits" (enrollments), where they become real prints.
 */
object LocalPrints {
    private fun file(ctx: Context) = File(ctx.filesDir, "phone_prints.json")

    fun load(ctx: Context): List<LocalVoice> {
        val f = file(ctx); if (!f.exists()) return emptyList()
        return try {
            val arr = JSONObject(f.readText()).getJSONArray("voices"); val out = ArrayList<LocalVoice>()
            for (i in 0 until arr.length()) { val v = arr.getJSONObject(i); val pr = v.getJSONArray("prints")
                out.add(LocalVoice(v.getInt("id"), v.optInt("person_id", 0).takeIf { it > 0 }, v.getString("name"), (0 until pr.length()).map { decode(pr.getString(it)) }, v.optLong("created_at", 0))) }
            out
        } catch (_: Exception) { emptyList() }
    }

    private fun save(ctx: Context, voices: List<LocalVoice>) {
        val arr = JSONArray()
        for (v in voices) arr.put(JSONObject().put("id", v.id).put("person_id", v.personId ?: 0).put("name", v.name).put("created_at", v.createdAt).put("prints", JSONArray(v.prints.map { encode(it) })))
        file(ctx).writeText(JSONObject().put("voices", arr).toString())
    }

    /** Add a print to an existing library person (or to the local voice that already targets them). */
    @Synchronized
    fun addToPerson(ctx: Context, personId: Int, name: String, emb: FloatArray) {
        val cur = load(ctx).toMutableList()
        val i = cur.indexOfFirst { (personId > 0 && it.personId == personId) || (personId < 0 && it.id == personId) }
        if (i >= 0) cur[i] = cur[i].copy(prints = cur[i].prints + emb)
        else cur.add(LocalVoice(nextId(cur), personId.takeIf { it > 0 }, name, listOf(emb), System.currentTimeMillis()))
        save(ctx, cur)
    }

    /** Enroll someone new on the phone. Returns the local (negative) id. */
    @Synchronized
    fun addNew(ctx: Context, name: String, emb: FloatArray): Int {
        val cur = load(ctx).toMutableList()
        val existing = cur.indexOfFirst { it.personId == null && it.name.equals(name.trim(), true) }
        if (existing >= 0) { cur[existing] = cur[existing].copy(prints = cur[existing].prints + emb); save(ctx, cur); return cur[existing].id }
        val id = nextId(cur)
        cur.add(LocalVoice(id, null, name.trim(), listOf(emb), System.currentTimeMillis())); save(ctx, cur); return id
    }

    @Synchronized
    fun remove(ctx: Context, localId: Int) { save(ctx, load(ctx).filter { it.id != localId }) }

    fun count(ctx: Context): Int = load(ctx).sumOf { it.prints.size }

    private fun nextId(cur: List<LocalVoice>) = (cur.minOfOrNull { it.id } ?: 0).coerceAtMost(0) - 1

    /** Library + phone prints. Local voices whose name matches a library person become extra prints for that person. */
    fun merge(g: Gallery, local: List<LocalVoice>): Gallery {
        if (local.isEmpty()) return g
        val people = g.people.toMutableList()
        val extra = HashMap<Int, MutableList<FloatArray>>()
        val fresh = ArrayList<Person>()
        for (v in local) {
            val target = people.firstOrNull { (v.personId != null && it.id == v.personId) || (v.personId == null && it.name.equals(v.name, true)) }
            if (target != null) extra.getOrPut(target.id) { ArrayList() }.addAll(v.prints.map { Gallery.normalise(it) })
            else fresh.add(Person(v.id, v.name, "named", true, v.prints.map { Gallery.normalise(it) }, local = true))
        }
        val merged = people.map { p -> extra[p.id]?.let { p.copy(prints = p.prints + it) } ?: p } + fresh
        return g.withPeople(merged)
    }

    /** JSON for the laptop (goes inside the phone-settings export). */
    fun exportJson(ctx: Context): JSONArray {
        val arr = JSONArray()
        for (v in load(ctx)) arr.put(JSONObject().put("person_id", v.personId ?: v.id).put("name", v.name).put("alert", v.personId == null).put("prints", JSONArray(v.prints.map { encode(it) })))
        return arr
    }

    fun encode(e: FloatArray): String {
        val bb = ByteBuffer.allocate(e.size * 4).order(ByteOrder.LITTLE_ENDIAN); for (v in e) bb.putFloat(v)
        return Base64.encodeToString(bb.array(), Base64.NO_WRAP)
    }
    private fun decode(b64: String): FloatArray {
        val fb = ByteBuffer.wrap(Base64.decode(b64, Base64.DEFAULT)).order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer()
        return FloatArray(fb.remaining()).also { fb.get(it) }
    }
}
