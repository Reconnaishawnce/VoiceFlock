package com.voiceflock.app

import android.content.Context
import android.util.Base64
import org.json.JSONObject
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.exp

data class Person(val id: Int, val name: String, val kind: String, val alertDefault: Boolean, val prints: List<FloatArray>)

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
) {
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
        fun file(context: Context): File = File(context.filesDir, "gallery.json")
        fun modelFile(context: Context): File = File(context.filesDir, "ecapa_voxceleb.onnx")

        fun load(context: Context): Gallery? {
            val f = file(context)
            if (!f.exists()) return null
            return try { parse(f.readText()) } catch (e: Exception) { null }
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
                if (prints.isNotEmpty())
                    people.add(Person(p.getInt("id"), p.getString("name"), p.optString("kind", "named"), p.optBoolean("alert", false), prints))
            }
            return Gallery(
                people, cal.getDouble("a"), cal.getDouble("b"), j.optDouble("match_threshold", 0.75).toFloat(),
                j.optDouble("window_s", 3.0).toFloat(), j.optDouble("hop_s", 1.0).toFloat(),
                j.optDouble("min_level_db", -45.0).toFloat(), j.optString("generated_at", ""), dim,
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
