package com.voiceflock.app

import android.content.Context
import android.content.SharedPreferences

/** Small settings store: server, password, per-voice alert overrides. */
class Prefs(context: Context) {
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

    /** Per-person alert flag set on the phone; absent = use the server's flag. */
    fun alertOverride(personId: Int): Boolean? =
        if (sp.contains("alert_$personId")) sp.getBoolean("alert_$personId", false) else null

    fun setAlertOverride(personId: Int, on: Boolean) = sp.edit().putBoolean("alert_$personId", on).apply()
    fun clearAlertOverride(personId: Int) = sp.edit().remove("alert_$personId").apply()
}
