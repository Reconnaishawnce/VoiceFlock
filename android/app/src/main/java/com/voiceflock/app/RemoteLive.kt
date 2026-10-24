package com.voiceflock.app

import android.util.Base64
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * The phone as a remote microphone: int16 PCM frames go to the laptop's /ws/live, which runs the full
 * model against the whole library and sends back the same frames the laptop's Live tab shows.
 */
class RemoteLive(private val prefs: Prefs, private val onFrame: (Detection) -> Unit, private val onStatus: (String) -> Unit) {
    private val client = OkHttpClient.Builder().pingInterval(20, TimeUnit.SECONDS).readTimeout(0, TimeUnit.MILLISECONDS).build()
    private var ws: WebSocket? = null
    private val ready = CountDownLatch(1)
    @Volatile var failed: String? = null; private set
    @Volatile var threshold = 0.75f; private set
    @Volatile var gallerySize = 0; private set
    private val people = HashMap<Int, Person>()

    fun connect(timeoutMs: Long = 10_000): Boolean {
        val base = prefs.serverUrl.trimEnd('/').replaceFirst("http://", "ws://").replaceFirst("https://", "wss://")
        val rb = Request.Builder().url("$base/ws/live")
        if (prefs.password.isNotEmpty()) rb.header("Authorization", "Basic " + Base64.encodeToString("phone:${prefs.password}".toByteArray(), Base64.NO_WRAP))
        ws = client.newWebSocket(rb.build(), object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val j = JSONObject(text)
                    when (j.optString("type")) {
                        "status" -> { if (j.has("threshold")) threshold = j.getDouble("threshold").toFloat(); if (j.has("gallery")) gallerySize = j.getInt("gallery"); onStatus(j.optString("text")); ready.countDown() }
                        "error" -> { failed = j.optString("text", "laptop error"); ready.countDown() }
                        "frame" -> onFrame(parseFrame(j))
                    }
                } catch (_: Exception) {}
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { failed = t.message ?: "connection lost"; ready.countDown() }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) { failed = failed ?: "closed: $reason"; ready.countDown() }
        })
        ready.await(timeoutMs, TimeUnit.MILLISECONDS)
        return failed == null && ready.count == 0L
    }

    fun send(pcm: ShortArray, n: Int) {
        val w = ws ?: return
        val bb = ByteBuffer.allocate(n * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (i in 0 until n) bb.putShort(pcm[i])
        if (!w.send(bb.array().toByteString())) failed = failed ?: "send failed"
    }

    fun close() { try { ws?.close(1000, "stop") } catch (_: Exception) {}; ws = null }

    private fun person(c: JSONObject): Person {
        val id = c.getInt("person_id")
        return people.getOrPut(id) {
            Person(id, c.optString("name", "#$id"), c.optString("kind", "named"), c.optBoolean("flagged", false), emptyList(),
                c.optString("alert_sound", "").ifEmpty { null }.takeIf { it != "null" }, c.optString("alert_message", "").ifEmpty { null }.takeIf { it != "null" })
        }
    }

    private fun parseFrame(j: JSONObject): Detection {
        val arr = j.optJSONArray("candidates")
        val top = ArrayList<Scored>()
        if (arr != null) for (i in 0 until arr.length()) { val c = arr.getJSONObject(i)
            top.add(Scored(person(c), c.optDouble("probability", 0.0).toFloat(), c.optDouble("instant", 0.0).toFloat(), c.optBoolean("flagged", false))) }
        return Detection(j.optDouble("t", 0.0).toFloat(), j.optDouble("level_db", -100.0).toFloat(), j.optBoolean("speech", false), top, j.optBoolean("alert", false) && top.isNotEmpty() && top[0].flagged)
    }
}
