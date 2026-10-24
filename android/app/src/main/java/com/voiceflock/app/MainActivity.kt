package com.voiceflock.app

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.GraphicEq
import androidx.compose.material.icons.rounded.People
import androidx.compose.material.icons.rounded.Tune
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.DelicateCoroutinesApi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.GlobalScope
import kotlinx.coroutines.launch

@OptIn(DelicateCoroutinesApi::class)
class MainActivity : ComponentActivity() {
    companion object { const val EXTRA_AUTOSTART = "autostart" }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        Alerts.ensureChannels(this)
        Alerts.initTts(this)
        if (AppState.gallery.value == null) AppState.gallery.value = Gallery.load(this)
        if (AppState.sightings.value.isEmpty()) AppState.sightings.value = Sightings.load(this)
        Sessions.load(this)
        handleShare(intent)
        handleAutostart(intent)
        autoFetch()
        setContent { VoiceFlockTheme { App() } }
    }

    /** Re-download the library from the user's link when the app opens (cheap: ETag). */
    private fun autoFetch() {
        val prefs = Prefs(this)
        if (!prefs.autoFetch || prefs.libraryUrl.isEmpty()) return
        GlobalScope.launch(Dispatchers.IO) {
            try { Sync.fetchLibraryUrl(this@MainActivity, prefs)?.let { reloadGallery(this@MainActivity); AppState.status.value = "library updated from link: ${it.people.size} voices" } } catch (_: Exception) {}
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
        handleAutostart(intent)
    }

    /** The quick-settings tile lands here: start listening if the microphone is already allowed. */
    private fun handleAutostart(intent: Intent?) {
        if (intent?.getBooleanExtra(EXTRA_AUTOSTART, false) != true) return
        intent.removeExtra(EXTRA_AUTOSTART)
        if (AppState.listening.value) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) ListenService.start(this)
    }

    /** "Share → VoiceFlock": a library file (from WhatsApp, email, Drive...) or a link to queue on the laptop. */
    private fun handleShare(intent: Intent?) {
        if (intent == null) return
        if (intent.getBooleanExtra("vf_handled", false)) return
        val uri: Uri? = when (intent.action) {
            Intent.ACTION_SEND -> @Suppress("DEPRECATION") intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
            Intent.ACTION_VIEW -> intent.data
            else -> null
        }
        val text = if (intent.action == Intent.ACTION_SEND) intent.getStringExtra(Intent.EXTRA_TEXT) else null
        intent.putExtra("vf_handled", true)
        when {
            uri != null -> importLibraryUri(this, uri)
            text != null && text.trimStart().startsWith("{") -> try { val g = Sync.importLibrary(this, text); reloadGallery(this); Toast.makeText(this, "Imported ${g.people.size} voices", Toast.LENGTH_LONG).show() } catch (e: Exception) { Toast.makeText(this, "Not a VoiceFlock library: ${e.message}", Toast.LENGTH_LONG).show() }
            text != null -> Regex("https?://\\S+").find(text)?.value?.let { queueLink(it) } ?: Toast.makeText(this, "No link in the shared text", Toast.LENGTH_SHORT).show()
        }
    }

    private fun queueLink(url: String) {
        val prefs = Prefs(this)
        if (!Sync.connected(prefs)) { Toast.makeText(this, "Set the laptop address in Settings first", Toast.LENGTH_LONG).show(); return }
        Toast.makeText(this, "Sending to the laptop…", Toast.LENGTH_SHORT).show()
        GlobalScope.launch(Dispatchers.IO) {
            try { val name = Sync.queueUrl(prefs, url, prefs.shareMode); runOnUiThread { Toast.makeText(this@MainActivity, "Queued on the laptop: $name", Toast.LENGTH_LONG).show() } }
            catch (e: Exception) { runOnUiThread { Toast.makeText(this@MainActivity, "Could not queue: ${e.message}", Toast.LENGTH_LONG).show() } }
        }
    }

    override fun onResume() {
        super.onResume()
        val prefs = Prefs(this)
        if (prefs.autoSync && Sync.connected(prefs)) GlobalScope.launch(Dispatchers.IO) {
            try { val r = Sync.syncAll(this@MainActivity, prefs); if (r != "recent" && r != "in sync") AppState.status.value = "sync: $r" } catch (_: Exception) {}
        }
    }
}

/** Reload library + phone prints; the service notices the new object and rebuilds its matcher. */
fun reloadGallery(ctx: Context) { AppState.gallery.value = Gallery.load(ctx) }

fun importLibraryUri(ctx: Context, uri: Uri) {
    try {
        val text = ctx.contentResolver.openInputStream(uri)!!.bufferedReader().readText()
        val g = Sync.importLibrary(ctx, text)
        reloadGallery(ctx)
        Prefs(ctx).lastSync = "file import " + java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.US).format(java.util.Date())
        Toast.makeText(ctx, "Imported ${g.people.size} voices", Toast.LENGTH_LONG).show()
    } catch (e: Exception) {
        Toast.makeText(ctx, "Not a VoiceFlock library file: ${e.message}", Toast.LENGTH_LONG).show()
    }
}

/** Ask for microphone (+ notifications on 13+) and run `go` once granted (immediately if already granted). */
@Composable
fun rememberMicPermission(go: () -> Unit): () -> Unit {
    val ctx = LocalContext.current
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { _ ->
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) go()
        else Toast.makeText(ctx, "VoiceFlock needs the microphone to listen.", Toast.LENGTH_LONG).show()
    }
    return {
        val need = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) need += Manifest.permission.POST_NOTIFICATIONS
        val missing = need.filter { ContextCompat.checkSelfPermission(ctx, it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isEmpty()) go() else launcher.launch(missing.toTypedArray())
    }
}

private enum class Tab(val label: String) { LISTEN("Listen"), VOICES("Voices"), SETTINGS("Settings") }

@Composable
fun App() {
    var tab by rememberSaveable { mutableStateOf(Tab.LISTEN) }
    val identify by AppState.identify.collectAsStateWithLifecycle()
    val colors = NavigationBarItemDefaults.colors(selectedIconColor = VF.AmberInk, indicatorColor = VF.Amber, selectedTextColor = VF.Fg, unselectedIconColor = VF.Muted, unselectedTextColor = VF.Muted)
    Scaffold(
        containerColor = VF.Bg,
        bottomBar = {
            NavigationBar(containerColor = VF.Surface, tonalElevation = 0.dp) {
                NavigationBarItem(tab == Tab.LISTEN, { tab = Tab.LISTEN }, { Icon(Icons.Rounded.GraphicEq, null) }, label = { Text("Listen") }, colors = colors)
                NavigationBarItem(tab == Tab.VOICES, { tab = Tab.VOICES }, { Icon(Icons.Rounded.People, null) }, label = { Text("Voices") }, colors = colors)
                NavigationBarItem(tab == Tab.SETTINGS, { tab = Tab.SETTINGS }, { Icon(Icons.Rounded.Tune, null) }, label = { Text("Settings") }, colors = colors)
            }
        },
    ) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            when (tab) {
                Tab.LISTEN -> ListenScreen(openVoices = { tab = Tab.VOICES }, openSettings = { tab = Tab.SETTINGS })
                Tab.VOICES -> VoicesScreen()
                Tab.SETTINGS -> SettingsScreen()
            }
        }
    }
    identify?.let { IdentifySheet(it) { AppState.identify.value = null } }
}
