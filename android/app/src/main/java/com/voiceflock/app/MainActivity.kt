package com.voiceflock.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Hearing
import androidx.compose.material.icons.filled.People
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Alerts.ensureChannels(this)
        if (AppState.gallery.value == null) AppState.gallery.value = Gallery.load(this)
        handleShare(intent)
        setContent { MaterialTheme(colorScheme = darkColorScheme(primary = Color(0xFF5AD1A8))) { App() } }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    /** "Share → VoiceFlock" or "Open with VoiceFlock" on a library file from WhatsApp, email, Drive... */
    private fun handleShare(intent: Intent?) {
        val uri: Uri = when (intent?.action) {
            Intent.ACTION_SEND -> @Suppress("DEPRECATION") intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
            Intent.ACTION_VIEW -> intent.data
            else -> null
        } ?: return
        importLibraryUri(this, uri)
    }
}

fun importLibraryUri(ctx: android.content.Context, uri: Uri) {
    try {
        val text = ctx.contentResolver.openInputStream(uri)!!.bufferedReader().readText()
        val g = Sync.importLibrary(ctx, text)
        AppState.gallery.value = g
        Prefs(ctx).lastSync = "file import " + java.text.SimpleDateFormat("yyyy-MM-dd HH:mm", java.util.Locale.US).format(java.util.Date())
        Toast.makeText(ctx, "Imported ${g.people.size} voices", Toast.LENGTH_LONG).show()
    } catch (e: Exception) {
        Toast.makeText(ctx, "Not a VoiceFlock library file: ${e.message}", Toast.LENGTH_LONG).show()
    }
}

private enum class Tab(val label: String) { LISTEN("Listen"), VOICES("Voices"), SETTINGS("Settings") }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun App() {
    var tab by remember { mutableStateOf(Tab.LISTEN) }
    Scaffold(
        topBar = { TopAppBar(title = { Text("VoiceFlock") }) },
        bottomBar = {
            NavigationBar {
                NavigationBarItem(tab == Tab.LISTEN, { tab = Tab.LISTEN }, { Icon(Icons.Filled.Hearing, null) }, label = { Text("Listen") })
                NavigationBarItem(tab == Tab.VOICES, { tab = Tab.VOICES }, { Icon(Icons.Filled.People, null) }, label = { Text("Voices") })
                NavigationBarItem(tab == Tab.SETTINGS, { tab = Tab.SETTINGS }, { Icon(Icons.Filled.Settings, null) }, label = { Text("Settings") })
            }
        },
    ) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            when (tab) {
                Tab.LISTEN -> ListenScreen()
                Tab.VOICES -> VoicesScreen()
                Tab.SETTINGS -> SettingsScreen()
            }
        }
    }
}

@Composable
fun ListenScreen() {
    val ctx = LocalContext.current
    val listening by AppState.listening.collectAsStateWithLifecycle()
    val status by AppState.status.collectAsStateWithLifecycle()
    val det by AppState.detection.collectAsStateWithLifecycle()
    val log by AppState.log.collectAsStateWithLifecycle()
    val gallery by AppState.gallery.collectAsStateWithLifecycle()
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { res ->
        if (res[Manifest.permission.RECORD_AUDIO] == true) ListenService.start(ctx)
    }
    Column(Modifier.padding(16.dp).fillMaxSize(), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Button(
            onClick = {
                if (listening) ListenService.stop(ctx) else {
                    val need = mutableListOf(Manifest.permission.RECORD_AUDIO)
                    if (Build.VERSION.SDK_INT >= 33) need += Manifest.permission.POST_NOTIFICATIONS
                    val missing = need.filter { ContextCompat.checkSelfPermission(ctx, it) != PackageManager.PERMISSION_GRANTED }
                    if (missing.isEmpty()) ListenService.start(ctx) else launcher.launch(missing.toTypedArray())
                }
            },
            modifier = Modifier.fillMaxWidth().height(56.dp),
            enabled = gallery != null,
        ) { Text(if (listening) "Stop listening" else "Start listening", fontSize = 18.sp) }
        Text(if (gallery == null) "No library yet: open Settings and sync with the laptop." else status, style = MaterialTheme.typography.bodyMedium)
        Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                val d = det
                when {
                    !listening -> Text("Not listening.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    d == null -> Text("warming up…", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    !d.speech -> Text("silence · ${"%.0f".format(d.levelDb)} dBFS", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    d.top.isEmpty() -> Text("speech · empty library")
                    else -> {
                        val top = d.top.first()
                        Text(if (d.alert) "FLAGGED VOICE" else if (top.flagged) "flagged, below threshold" else "closest voice",
                            style = MaterialTheme.typography.labelMedium, color = if (d.alert) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(top.person.name, fontSize = 28.sp, fontWeight = FontWeight.Bold)
                        LinearProgressIndicator(progress = { top.probability }, modifier = Modifier.fillMaxWidth())
                        Text("${(top.probability * 100).toInt()}%  ·  " + d.top.drop(1).joinToString(" · ") { "${it.person.name} ${(it.probability * 100).toInt()}%" },
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
        Text("Alerts", style = MaterialTheme.typography.titleSmall)
        LazyColumn(Modifier.weight(1f)) { items(log) { Text(it, Modifier.padding(vertical = 4.dp)) } }
    }
}

@Composable
fun VoicesScreen() {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val gallery by AppState.gallery.collectAsStateWithLifecycle()
    var alertAll by remember { mutableStateOf(prefs.alertAllKnown) }
    var version by remember { mutableIntStateOf(0) }
    var filter by remember { mutableStateOf("") }
    val g = gallery
    Column(Modifier.padding(16.dp).fillMaxSize()) {
        if (g == null) { Text("Sync first."); return }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Alert on every known voice", Modifier.weight(1f))
            Switch(alertAll, { alertAll = it; prefs.alertAllKnown = it })
        }
        OutlinedTextField(filter, { filter = it }, label = { Text("search") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        Text("${g.people.size} voices · flagged ones alert even when the phone is in your pocket. Changes apply the next time you press Start.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(vertical = 8.dp))
        key(version) {
            LazyColumn {
                val list = g.people.filter { it.name.contains(filter, true) }.sortedWith(compareBy({ it.kind != "named" }, { it.name }))
                items(list, key = { it.id }) { p ->
                    val on = prefs.alertOverride(p.id) ?: (p.alertDefault || (alertAll && p.kind == "named"))
                    Row(Modifier.fillMaxWidth().padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text(p.name, fontWeight = FontWeight.Medium)
                            Text(if (p.kind == "named") "known · ${p.prints.size} prints" else "unnamed · ${p.prints.size} prints",
                                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Switch(on, { prefs.setAlertOverride(p.id, it); version++ })
                    }
                }
            }
        }
    }
}

fun modelLine(ctx: android.content.Context): String {
    val f = Gallery.modelFile(ctx)
    return if (f.exists()) "Speaker model: ready (${f.length() shr 20} MB)" else "Speaker model: not downloaded yet"
}

@Composable
fun SettingsScreen() {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val scope = rememberCoroutineScope()
    var url by remember { mutableStateOf(prefs.serverUrl) }
    var pw by remember { mutableStateOf(prefs.password) }
    var status by remember { mutableStateOf(if (prefs.lastSync.isEmpty()) "never synced" else "last sync ${prefs.lastSync}") }
    var busy by remember { mutableStateOf(false) }
    var thr by remember { mutableFloatStateOf(prefs.thresholdOverride) }
    val gallery by AppState.gallery.collectAsStateWithLifecycle()
    var modelStatus by remember { mutableStateOf(modelLine(ctx)) }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) { importLibraryUri(ctx, uri); status = "library imported from file" }
    }
    Column(Modifier.padding(16.dp).fillMaxSize().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text("No network needed: two files", style = MaterialTheme.typography.titleSmall)
        Text("1. Voice library: on the laptop, Settings → \"Download phone library\", send the .json to this phone (WhatsApp, email, Drive) and open it with VoiceFlock, or pick it here.\n2. Speaker model: one 85 MB download from GitHub.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { picker.launch(arrayOf("application/json", "application/octet-stream", "text/plain", "*/*")) }, modifier = Modifier.weight(1f)) { Text("Import library file") }
            OutlinedButton(enabled = !busy, onClick = {
                busy = true
                scope.launch {
                    try { Sync.downloadModelFromInternet(ctx) { modelStatus = it } } catch (e: Exception) { modelStatus = "model download failed: ${e.message}" }
                    modelStatus = modelLine(ctx); busy = false
                }
            }, modifier = Modifier.weight(1f)) { Text("Download model") }
        }
        Text(modelStatus, style = MaterialTheme.typography.bodySmall)
        HorizontalDivider()
        Text("Or sync from the laptop directly (same Wi-Fi or Tailscale)", style = MaterialTheme.typography.titleSmall)
        OutlinedTextField(url, { url = it }, label = { Text("VoiceFlock address, e.g. http://192.168.1.20:8787 or your Tailscale name") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(pw, { pw = it }, label = { Text("Password (if set in .env)") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        Button(
            enabled = !busy,
            onClick = {
                prefs.serverUrl = url; prefs.password = pw; busy = true
                scope.launch {
                    try {
                        val g = Sync.run(ctx, prefs) { status = it }
                        AppState.gallery.value = g
                    } catch (e: Exception) { status = "sync failed: ${e.message}" }
                    modelStatus = modelLine(ctx); busy = false
                }
            }, modifier = Modifier.fillMaxWidth()) { Text(if (busy) "Syncing…" else "Sync voices + model") }
        Text(status, style = MaterialTheme.typography.bodyMedium)
        gallery?.let {
            Text("Library: ${it.people.size} voices, ${it.printCount} prints, generated ${it.generatedAt.take(16).replace('T', ' ')}. Server threshold ${(it.threshold * 100).toInt()}%.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        HorizontalDivider()
        Text("Matching", style = MaterialTheme.typography.titleSmall)
        Text(if (thr <= 0f) "Match threshold: use the laptop's setting" else "Match threshold override: ${(thr * 100).toInt()}%")
        Slider(value = thr, onValueChange = { thr = it }, onValueChangeFinished = { prefs.thresholdOverride = if (thr < 0.3f) 0f else thr }, valueRange = 0f..0.99f)
        Text("Drag below 30% to use the laptop's value. Alerts repeat for the same voice at most every ${prefs.cooldownSeconds}s.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        HorizontalDivider()
        Text("Everything runs on this phone. After a sync no network is needed; audio never leaves the device and is not stored.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
