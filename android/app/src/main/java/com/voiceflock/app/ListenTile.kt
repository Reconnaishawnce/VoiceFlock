package com.voiceflock.app

import android.app.PendingIntent
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

/** Quick-settings tile: one tap starts or stops listening without opening the app. */
class ListenTile : TileService() {
    companion object {
        fun refresh(ctx: Context) { try { requestListeningState(ctx, ComponentName(ctx, ListenTile::class.java)) } catch (_: Exception) {} }
    }

    override fun onStartListening() { super.onStartListening(); update() }

    private fun update() {
        val t = qsTile ?: return
        val on = AppState.listening.value
        t.state = if (on) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        t.label = "VoiceFlock"
        if (Build.VERSION.SDK_INT >= 29) t.subtitle = if (on) "listening" else "off"
        t.updateTile()
    }

    override fun onClick() {
        super.onClick()
        if (AppState.listening.value) { ListenService.stop(this); update(); return }
        // starting a microphone service straight from a tile is restricted on newer Android; go through the activity
        val i = Intent(this, MainActivity::class.java).putExtra(MainActivity.EXTRA_AUTOSTART, true)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        if (Build.VERSION.SDK_INT >= 34) startActivityAndCollapse(PendingIntent.getActivity(this, 7, i, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
        else @Suppress("DEPRECATION") startActivityAndCollapse(i)
    }
}
