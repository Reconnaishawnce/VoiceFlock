package com.voiceflock.app

import android.content.Context
import android.util.Base64
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.exp

data class Person(
    val id: Int, val name: String, val kind: String, val alertDefault: Boolean, val prints: List<FloatArray>,
    val alertSound: String? = null,      // siren | horn | vault | radar | klaxon | chime | none | null = inherit
    val alertMessage: String? = null,    // template with [NAME]; null = inherit
    val groupIds: List<Int> = emptyList(),
    val local: Boolean = false,          // enrolled on this phone (negative id), not yet in the laptop's library
    val photo: String? = null,           // base64 JPEG thumbnail from the laptop's dossier, if any
    val role: String? = null,
)

data class Group(val id: Int, val name: String, val description: String?, val alert: Boolean, val alertSound: String?, val alertMessage: String?, val n: Int)

data class Match(val person: Person, val cosine: Float, val probability: Float)

/** The voiceprint library synced from the laptop, plus the maths to score an embedding against it. */
class Gallery(
    val people: List<Person>,
    val calibA: Double,
    val calibB: Double,
    val threshold: Float,
    val windowS: Float,
    val hopS: Float,
    val minLevelDb: Float,
    val generatedAt: String,
    val dim: Int,
    val defaultMessage: String = DEFAULT_MESSAGE,
    val defaultSound: String = "siren",
    val groups: List<Group> = emptyList(),
) {
    val groupById: Map<Int, Group> = groups.associateBy { it.id }
    private val owners: IntArray
    private val matrix: Array<FloatArray>

    init {
        val o = ArrayList<Int>()
        val m = ArrayList<FloatArray>()
        people.forEachIndexed { i, p -> p.prints.forEach { e -> o.add(i); m.add(e) } }
        owners = o.toIntArray()
        matrix = m.toTypedArray()
    }

    val printCount get() = matrix.size

    fun withPeople(p: List<Person>) = Gallery(p, calibA, calibB, threshold, windowS, hopS, minLevelDb, generatedAt, dim, defaultMessage, defaultSound, groups)

    fun probability(cosine: Float): Float = (1.0 / (1.0 + exp(-calibA * (cosine + calibB)))).toFloat()

    /** Best print per person, ranked by cosine; top k. */
    fun topMatches(emb: FloatArray, k: Int = 3): List<Match> {
        if (matrix.isEmpty()) return emptyList()
        val best = FloatArray(people.size) { -2f }
        for (i in matrix.indices) {
            val row = matrix[i]
            var s = 0f
            for (j in row.indices) s += row[j] * emb[j]
            val o = owners[i]
            if (s > best[o]) best[o] = s
        }
        return best.indices.filter { best[it] > -2f }
            .sortedByDescending { best[it] }
            .take(k)
            .map { Match(people[it], best[it], probability(best[it])) }
    }

    companion object {
        const val DEFAULT_MESSAGE = "Attention. [NAME], your voice was detected nearby. It's time to regulate mass surveillance."
        val SOUNDS = listOf("siren" to "siren (civil-defence wail)", "horn" to "horn (emergency two-tone)", "vault" to "vault (blast door)", "radar" to "radar (phone alarm)", "klaxon" to "klaxon", "chime" to "chime", "none" to "no sound")
        fun render(template: String, name: String) = template.replace("[NAME]", name).replace("{name}", name).trim()

        fun file(context: Context): File = File(context.filesDir, "gallery.json")
        fun modelFile(context: Context): File = File(context.filesDir, "ecapa_voxceleb.onnx")

        fun load(context: Context): Gallery? {
            val f = file(context)
            if (!f.exists()) return null
            return try { LocalPrints.merge(parse(f.readText()), LocalPrints.load(context)) } catch (e: Exception) { null }
        }

        fun parse(text: String): Gallery {
            val j = JSONObject(text)
            require(j.optString("format") == "voiceflock-mobile") { "not a VoiceFlock mobile gallery" }
            val dim = j.optInt("dim", 192)
            val cal = j.getJSONObject("calibration")
            val arr = j.getJSONArray("people")
            val people = ArrayList<Person>(arr.length())
            for (i in 0 until arr.length()) {
                val p = arr.getJSONObject(i)
                val pr = p.getJSONArray("prints")
                val prints = ArrayList<FloatArray>(pr.length())
                for (k in 0 until pr.length()) {
                    val e = decode(pr.getString(k))
                    if (e.size == dim) prints.add(normalise(e))
                }
                val gids = ArrayList<Int>()
                p.optJSONArray("group_ids")?.let { a -> for (k in 0 until a.length()) gids.add(a.getInt(k)) }
                if (prints.isNotEmpty())
                    people.add(Person(p.getInt("id"), p.getString("name"), p.optString("kind", "named"), p.optBoolean("alert", false), prints,
                        p.optString("alert_sound", "").ifEmpty { null }.takeIf { it != "null" },
                        p.optString("alert_message", "").ifEmpty { null }.takeIf { it != "null" }, gids,
                        photo = p.optString("photo", "").ifEmpty { null }.takeIf { it != "null" }, role = p.optString("role", "").ifEmpty { null }.takeIf { it != "null" }))
            }
            val groups = ArrayList<Group>()
            j.optJSONArray("groups")?.let { a -> for (k in 0 until a.length()) { val g = a.getJSONObject(k)
                groups.add(Group(g.getInt("id"), g.getString("name"), g.optString("description", "").ifEmpty { null }.takeIf { it != "null" }, g.optBoolean("alert", false),
                    g.optString("alert_sound", "").ifEmpty { null }.takeIf { it != "null" }, g.optString("alert_message", "").ifEmpty { null }.takeIf { it != "null" }, g.optInt("n", 0))) } }
            val defaults = j.optJSONObject("alert_defaults")
            return Gallery(
                people, cal.getDouble("a"), cal.getDouble("b"), j.optDouble("match_threshold", 0.75).toFloat(),
                j.optDouble("window_s", 4.0).toFloat(), j.optDouble("hop_s", 1.0).toFloat(),
                j.optDouble("min_level_db", -45.0).toFloat(), j.optString("generated_at", ""), dim,
                defaults?.optString("message", DEFAULT_MESSAGE)?.ifEmpty { DEFAULT_MESSAGE } ?: DEFAULT_MESSAGE,
                defaults?.optString("sound", "siren")?.ifEmpty { "siren" } ?: "siren",
                groups,
            )
        }

        private fun decode(b64: String): FloatArray {
            val bytes = Base64.decode(b64, Base64.DEFAULT)
            val fb = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer()
            val out = FloatArray(fb.remaining())
            fb.get(out)
            return out
        }

        fun normalise(e: FloatArray): FloatArray {
            var n = 0f
            for (v in e) n += v * v
            n = kotlin.math.sqrt(n) + 1e-9f
            return FloatArray(e.size) { e[it] / n }
        }
    }
}
