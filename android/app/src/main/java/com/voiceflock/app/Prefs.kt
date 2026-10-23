package com.voiceflock.app

import android.content.Context
import android.content.SharedPreferences

/** Small settings store: server, password, per-voice alert overrides. */
class Prefs(context: Context) {
    private val ctx: Context = context.applicationContext
    private val sp: SharedPreferences = context.getSharedPreferences("voiceflock", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = sp.getString("server_url", "http://192.168.1.10:8787") ?: ""
        set(v) = sp.edit().putString("server_url", v.trim().trimEnd('/')).apply()

    var password: String
        get() = sp.getString("password", "") ?: ""
        set(v) = sp.edit().putString("password", v).apply()

    /** Alert on every known (named) voice, not only the flagged ones. */
    var alertAllKnown: Boolean
        get() = sp.getBoolean("alert_all_known", false)
        set(v) = sp.edit().putBoolean("alert_all_known", v).apply()

    /** Optional local override of the server's match threshold (0 = use server value). */
    var thresholdOverride: Float
        get() = sp.getFloat("threshold_override", 0f)
        set(v) = sp.edit().putFloat("threshold_override", v).apply()

    var cooldownSeconds: Int
        get() = sp.getInt("cooldown_s", 30)
        set(v) = sp.edit().putInt("cooldown_s", v).apply()

    var lastSync: String
        get() = sp.getString("last_sync", "") ?: ""
        set(v) = sp.edit().putString("last_sync", v).apply()

    /** Demo mode: alarm + speech at full volume on the alarm channel, even when the phone is silent. */
    var demoMode: Boolean
        get() = sp.getBoolean("demo_mode", true)
        set(v) = sp.edit().putBoolean("demo_mode", v).apply()

    /** Phone-side default spoken message; empty = use the library's default. */
    var defaultMessage: String
        get() = sp.getString("default_message", "") ?: ""
        set(v) = sp.edit().putString("default_message", v).apply()

    fun messageOverride(personId: Int): String? = sp.getString("msg_$personId", null)
    fun setMessageOverride(personId: Int, msg: String?) =
        sp.edit().apply { if (msg.isNullOrBlank()) remove("msg_$personId") else putString("msg_$personId", msg) }.apply()
    fun soundOverride(personId: Int): String? = sp.getString("snd_$personId", null)
    fun setSoundOverride(personId: Int, snd: String?) =
        sp.edit().apply { if (snd.isNullOrBlank()) remove("snd_$personId") else putString("snd_$personId", snd) }.apply()

    // ---- package (group) overrides made on the phone
    fun groupAlertOverride(gid: Int): Boolean? = if (sp.contains("galert_$gid")) sp.getBoolean("galert_$gid", false) else null
    fun setGroupAlertOverride(gid: Int, on: Boolean) = sp.edit().putBoolean("galert_$gid", on).apply()
    fun groupSoundOverride(gid: Int): String? = sp.getString("gsnd_$gid", null)
    fun setGroupSoundOverride(gid: Int, v: String?) = sp.edit().apply { if (v.isNullOrBlank()) remove("gsnd_$gid") else putString("gsnd_$gid", v) }.apply()
    fun groupMessageOverride(gid: Int): String? = sp.getString("gmsg_$gid", null)
    fun setGroupMessageOverride(gid: Int, v: String?) = sp.edit().apply { if (v.isNullOrBlank()) remove("gmsg_$gid") else putString("gmsg_$gid", v) }.apply()

    fun groupAlert(g: Group): Boolean = groupAlertOverride(g.id) ?: g.alert
    fun groupSound(g: Group): String? = groupSoundOverride(g.id) ?: g.alertSound
    fun groupMessage(g: Group): String? = groupMessageOverride(g.id) ?: g.alertMessage

    /** Does this voice alert? phone person override > person flag > any alerting package > "alert all known". */
    fun isFlagged(p: Person, g: Gallery): Boolean {
        alertOverride(p.id)?.let { return it }
        if (p.alertDefault) return true
        if (p.groupIds.any { gid -> g.groupById[gid]?.let { groupAlert(it) } == true }) return true
        return alertAllKnown && p.kind == "named"
    }

    /** Resolve what to play/say: phone person override > person > first package with a value > phone default > library default. */
    fun soundFor(p: Person, g: Gallery): String =
        soundOverride(p.id) ?: p.alertSound ?: p.groupIds.firstNotNullOfOrNull { gid -> g.groupById[gid]?.let { groupSound(it) } } ?: g.defaultSound
    fun messageFor(p: Person, g: Gallery): String = Gallery.render(
        messageOverride(p.id) ?: p.alertMessage ?: p.groupIds.firstNotNullOfOrNull { gid -> g.groupById[gid]?.let { groupMessage(it) } }
            ?: defaultMessage.ifBlank { g.defaultMessage }, p.name)

    /** Where the library can be fetched from without the laptop (Dropbox / Drive / GitHub link). */
    var libraryUrl: String
        get() = sp.getString("library_url", "") ?: ""
        set(v) = sp.edit().putString("library_url", v.trim()).apply()
    var autoFetch: Boolean
        get() = sp.getBoolean("auto_fetch", true)
        set(v) = sp.edit().putBoolean("auto_fetch", v).apply()
    var libraryEtag: String
        get() = sp.getString("library_etag", "") ?: ""
        set(v) = sp.edit().putString("library_etag", v).apply()

    /** Everything edited on this phone, as JSON the laptop can import. */
    fun exportEdits(g: Gallery?): String {
        val persons = org.json.JSONObject(); val groups = org.json.JSONObject()
        for ((k, v) in sp.all) {
            val id = k.substringAfter("_", "").toIntOrNull() ?: continue
            if (id < 0) continue
            when {
                k.startsWith("alert_") -> persons.optJSONObject(id.toString())?.put("alert", v) ?: persons.put(id.toString(), org.json.JSONObject().put("alert", v))
                k.startsWith("snd_") -> persons.optJSONObject(id.toString())?.put("sound", v) ?: persons.put(id.toString(), org.json.JSONObject().put("sound", v))
                k.startsWith("msg_") -> persons.optJSONObject(id.toString())?.put("message", v) ?: persons.put(id.toString(), org.json.JSONObject().put("message", v))
                k.startsWith("galert_") -> groups.optJSONObject(id.toString())?.put("alert", v) ?: groups.put(id.toString(), org.json.JSONObject().put("alert", v))
                k.startsWith("gsnd_") -> groups.optJSONObject(id.toString())?.put("sound", v) ?: groups.put(id.toString(), org.json.JSONObject().put("sound", v))
                k.startsWith("gmsg_") -> groups.optJSONObject(id.toString())?.put("message", v) ?: groups.put(id.toString(), org.json.JSONObject().put("message", v))
            }
        }
        return org.json.JSONObject().put("format", "voiceflock-phone-settings").put("library_generated_at", g?.generatedAt ?: "")
            .put("persons", persons).put("groups", groups).put("enrollments", LocalPrints.exportJson(ctx)).toString(1)
    }

    /** Per-person alert flag set on the phone; absent = use the server's flag. */
    fun alertOverride(personId: Int): Boolean? =
        if (sp.contains("alert_$personId")) sp.getBoolean("alert_$personId", false) else null

    fun setAlertOverride(personId: Int, on: Boolean) = sp.edit().putBoolean("alert_$personId", on).apply()
    fun clearAlertOverride(personId: Int) = sp.edit().remove("alert_$personId").apply()
}
