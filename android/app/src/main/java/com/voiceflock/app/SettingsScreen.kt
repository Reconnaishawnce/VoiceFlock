package com.voiceflock.app

import android.app.StatusBarManager
import android.content.ComponentName
import android.content.Intent
import android.graphics.drawable.Icon
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.launch

fun modelLine(ctx: android.content.Context): String {
    val f = Gallery.modelFile(ctx)
    return if (f.exists()) "ready · ${f.length() shr 20} MB" else "not downloaded yet"
}

private val btnShape = RoundedCornerShape(12.dp)
private val fieldShape = RoundedCornerShape(12.dp)

@Composable
fun SettingsScreen() {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val scope = rememberCoroutineScope()
    val gallery by AppState.gallery.collectAsStateWithLifecycle()
    var status by remember { mutableStateOf(if (prefs.lastSync.isEmpty()) "never synced" else "last sync ${prefs.lastSync}") }
    var busy by remember { mutableStateOf(false) }
    var modelStatus by remember { mutableStateOf(modelLine(ctx)) }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) { importLibraryUri(ctx, uri); status = "library imported from file" }
    }
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp, 8.dp, 16.dp, 24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Settings", style = MaterialTheme.typography.headlineMedium)

        // ---------------------------------------------------------------- library
        Section("Voice library", trailing = { gallery?.let { Pill("${it.people.size} voices") } }) {
            gallery?.let { g ->
                Hint("${g.printCount} prints · ${g.groups.size} packages · made ${g.generatedAt.take(16).replace('T', ' ')} · threshold ${(g.threshold * 100).toInt()}%" +
                    (LocalPrints.count(ctx).takeIf { it > 0 }?.let { " · $it prints made on this phone" } ?: ""))
            } ?: Hint("Nothing yet. The library is a small .json the laptop makes: Settings → Download phone library. Send it here any way you like.")
            Hint(status)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { picker.launch(arrayOf("application/json", "application/octet-stream", "text/plain", "*/*")) }, modifier = Modifier.weight(1f), shape = btnShape) { Text("Import file") }
                OutlinedButton(enabled = gallery != null, modifier = Modifier.weight(1f), shape = btnShape, onClick = {
                    val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_SUBJECT, "VoiceFlock library").putExtra(Intent.EXTRA_TEXT, Gallery.file(ctx).readText())
                    ctx.startActivity(Intent.createChooser(send, "Share library"))
                }) { Text("Share library") }
            }
            Divider()
            Label("Hands-free: library link")
            Hint("Laptop: Settings → Auto-export phone library to a Dropbox / Drive / OneDrive folder, share that file's link, paste it here. The phone re-downloads it whenever it opens.")
            var libUrl by remember { mutableStateOf(prefs.libraryUrl) }
            var autoFetch by remember { mutableStateOf(prefs.autoFetch) }
            OutlinedTextField(libUrl, { libUrl = it }, placeholder = { Text("https://…") }, label = { Text("Library link") }, modifier = Modifier.fillMaxWidth(), singleLine = true, shape = fieldShape)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Button(enabled = !busy && libUrl.isNotBlank(), shape = btnShape, onClick = {
                    prefs.libraryUrl = libUrl; busy = true
                    scope.launch {
                        try { val g = Sync.fetchLibraryUrl(ctx, prefs, force = true); if (g != null) { reloadGallery(ctx); status = "fetched ${g.people.size} voices, ${g.groups.size} packages" } else status = "unchanged" }
                        catch (e: Exception) { status = "fetch failed: ${e.message}" }
                        busy = false
                    }
                }) { Text("Fetch now") }
                Spacer(Modifier.width(14.dp)); Text("Auto-refresh", Modifier.weight(1f)); Switch(autoFetch, { autoFetch = it; prefs.autoFetch = it }, colors = switchColors())
            }
        }

        // ---------------------------------------------------------------- laptop connection
        Section("Laptop connection", trailing = { if (Sync.connected(prefs)) Pill("set", VF.Ok, VF.Ok.copy(alpha = .14f)) else Pill("off") }) {
            Hint("Works from anywhere with Tailscale on both devices (free): the laptop's Settings → Phone shows the address to paste here. On the same Wi-Fi the http://192.168… address works too. With a connection the phone keeps itself in step: voices and alert edits both ways, your detections and the laptop's in one Heard list, and links you share to VoiceFlock get queued on the laptop.")
            var url by remember { mutableStateOf(if (Sync.connected(prefs)) prefs.serverUrl else "") }
            var pw by remember { mutableStateOf(prefs.password) }
            var test by remember { mutableStateOf(prefs.lastLaptop) }
            var auto by remember { mutableStateOf(prefs.autoSync) }
            var share by remember { mutableStateOf(prefs.shareMode) }
            OutlinedTextField(url, { url = it }, label = { Text("Laptop address") }, placeholder = { Text("http://thinkpad.tailxxxx.ts.net:8787") }, modifier = Modifier.fillMaxWidth(), singleLine = true, shape = fieldShape)
            OutlinedTextField(pw, { pw = it }, label = { Text("Password (VOICEFLOCK_PASSWORD in the laptop's .env)") }, modifier = Modifier.fillMaxWidth(), singleLine = true, shape = fieldShape)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(enabled = !busy && url.isNotBlank(), modifier = Modifier.weight(1f), shape = btnShape, onClick = {
                    prefs.serverUrl = url; prefs.password = pw; busy = true; test = "testing…"
                    scope.launch { test = try { "Connected: " + Sync.health(prefs) } catch (e: Exception) { "Not reachable: ${e.message}" }; busy = false }
                }) { Text("Test") }
                Button(enabled = !busy && url.isNotBlank(), modifier = Modifier.weight(1f), shape = btnShape, onClick = {
                    prefs.serverUrl = url; prefs.password = pw; busy = true
                    scope.launch { status = try { "sync: " + Sync.syncAll(ctx, prefs, force = true) } catch (e: Exception) { "sync failed: ${e.message}" }; modelStatus = modelLine(ctx); busy = false }
                }) { Text(if (busy) "Working…" else "Sync now") }
            }
            if (test.isNotEmpty()) Hint(test)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { TwoLine("Keep in sync automatically", "When the app opens and every 30 s while listening.") }
                Switch(auto, { auto = it; prefs.autoSync = it }, colors = switchColors())
            }
            TwoLine("Links shared to VoiceFlock go to", null)
            @OptIn(ExperimentalMaterial3Api::class)
            SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                listOf("bulk" to "Library", "review" to "Review queue").forEachIndexed { i, (v, label) ->
                    SegmentedButton(share == v, { share = v; prefs.shareMode = v }, SegmentedButtonDefaults.itemShape(i, 2),
                        colors = SegmentedButtonDefaults.colors(activeContainerColor = VF.Amber.copy(alpha = .18f), activeContentColor = VF.Amber, inactiveContainerColor = VF.Surface2, inactiveContentColor = VF.Fg2, activeBorderColor = VF.Line, inactiveBorderColor = VF.Line)) { Text(label) }
                }
            }
            Hint("Share a YouTube link from any app → VoiceFlock, and the laptop downloads and processes it.")
        }

        // ---------------------------------------------------------------- model
        Section("Speaker model", trailing = { Pill(modelStatus, if (Gallery.modelFile(ctx).exists()) VF.Ok else VF.Amber) }) {
            Hint("The same ECAPA encoder the laptop uses, as an 85 MB ONNX file. One download from GitHub; nothing leaves the phone afterwards.")
            OutlinedButton(enabled = !busy, modifier = Modifier.fillMaxWidth(), shape = btnShape, onClick = {
                busy = true
                scope.launch {
                    try { Sync.downloadModelFromInternet(ctx) { modelStatus = it } } catch (e: Exception) { modelStatus = "failed: ${e.message}" }
                    modelStatus = modelLine(ctx); busy = false
                }
            }) { Text(if (Gallery.modelFile(ctx).exists()) "Re-download model" else "Download model") }
        }

        // ---------------------------------------------------------------- alerts
        Section("Alerts") {
            var demo by remember { mutableStateOf(prefs.demoMode) }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { TwoLine("Demo mode", "Alarm and spoken line at full volume on the alarm channel, even on silent.") }
                Switch(demo, { demo = it; prefs.demoMode = it }, colors = switchColors())
            }
            var defMsg by remember { mutableStateOf(prefs.defaultMessage) }
            OutlinedTextField(defMsg, { defMsg = it; prefs.defaultMessage = it }, label = { Text("Default spoken line · [NAME] = the person") },
                placeholder = { Text(gallery?.defaultMessage ?: Gallery.DEFAULT_MESSAGE) }, modifier = Modifier.fillMaxWidth(), minLines = 2, shape = fieldShape)
            OutlinedButton(modifier = Modifier.fillMaxWidth(), shape = btnShape, onClick = {
                val g = gallery
                Alerts.performAlert(ctx, "Nancy Pelosi", g?.defaultSound ?: "siren", Gallery.render(defMsg.ifBlank { g?.defaultMessage ?: Gallery.DEFAULT_MESSAGE }, "Nancy Pelosi"), prefs.demoMode)
            }) { Text("▶ Test alarm + voice") }
        }

        // ---------------------------------------------------------------- matching
        Section("Matching") {
            var thr by remember { mutableFloatStateOf(prefs.thresholdOverride) }
            TwoLine(if (thr <= 0f) "Match threshold: laptop's (${((gallery?.threshold ?: .75f) * 100).toInt()}%)" else "Match threshold: ${(thr * 100).toInt()}%",
                "Below 30% means use the laptop's value. Higher = fewer false alarms, more misses.")
            Slider(value = thr, onValueChange = { thr = it }, onValueChangeFinished = { prefs.thresholdOverride = if (thr < 0.3f) 0f else thr }, valueRange = 0f..0.99f,
                colors = SliderDefaults.colors(thumbColor = VF.Amber, activeTrackColor = VF.Amber, inactiveTrackColor = VF.Surface3))
            var cd by remember { mutableIntStateOf(prefs.cooldownSeconds) }
            TwoLine("Repeat alerts for the same voice at most every", null)
            @OptIn(ExperimentalMaterial3Api::class)
            SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                listOf(15, 30, 60, 120, 300).forEachIndexed { i, v ->
                    SegmentedButton(cd == v, { cd = v; prefs.cooldownSeconds = v }, SegmentedButtonDefaults.itemShape(i, 5),
                        colors = SegmentedButtonDefaults.colors(activeContainerColor = VF.Amber.copy(alpha = .18f), activeContentColor = VF.Amber, inactiveContainerColor = VF.Surface2, inactiveContentColor = VF.Fg2, activeBorderColor = VF.Line, inactiveBorderColor = VF.Line)) {
                        Text(if (v < 60) "${v}s" else "${v / 60}m")
                    }
                }
            }
        }

        // ---------------------------------------------------------------- background
        Section("Keep it running") {
            val pm = ctx.getSystemService(PowerManager::class.java)
            var unrestricted by remember { mutableStateOf(pm.isIgnoringBatteryOptimizations(ctx.packageName)) }
            val owner = LocalLifecycleOwner.current
            DisposableEffect(owner) {
                val obs = LifecycleEventObserver { _, e -> if (e == Lifecycle.Event.ON_RESUME) unrestricted = pm.isIgnoringBatteryOptimizations(ctx.packageName) }
                owner.lifecycle.addObserver(obs); onDispose { owner.lifecycle.removeObserver(obs) }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { TwoLine("Battery: ${if (unrestricted) "unrestricted" else "optimised"}", if (unrestricted) "Android will leave the listener alone." else "Some phones stop background microphones after a while. Allow unrestricted battery use to prevent that.") }
                if (!unrestricted) TextButton(onClick = {
                    try { ctx.startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).setData(Uri.parse("package:${ctx.packageName}"))) }
                    catch (_: Exception) { ctx.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)) }
                }) { Text("Allow") }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { TwoLine("Quick-settings tile", "Start or stop listening from the notification shade without opening the app.") }
                TextButton(onClick = {
                    if (Build.VERSION.SDK_INT >= 33) {
                        ctx.getSystemService(StatusBarManager::class.java).requestAddTileService(ComponentName(ctx, ListenTile::class.java), "VoiceFlock",
                            Icon.createWithResource(ctx, R.drawable.ic_stat_wave), { it.run() }) { }
                    } else Toast.makeText(ctx, "Pull down the shade, tap the pencil, drag the VoiceFlock tile in.", Toast.LENGTH_LONG).show()
                }) { Text("Add tile") }
            }
            Hint("Expect roughly 5–10% battery per hour of active listening; nothing is used while the phone hears silence.")
        }

        // ---------------------------------------------------------------- back to laptop
        Section("Send back to the laptop") {
            Hint("Flags, sounds, lines and any voices you added with Identify, as text. Paste it into the laptop's Settings → “Bring edits made on the phone back here”.")
            OutlinedButton(modifier = Modifier.fillMaxWidth(), shape = btnShape, onClick = {
                val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_SUBJECT, "VoiceFlock phone edits").putExtra(Intent.EXTRA_TEXT, prefs.exportEdits(gallery))
                ctx.startActivity(Intent.createChooser(send, "Share my edits"))
            }) { Text("Share my edits") }
        }

        Hint("VoiceFlock ${BuildConfig.VERSION_NAME} · Everything runs on this phone. After a sync no network is needed; audio is never stored or sent anywhere. Check the recording-consent law where you are before listening to people continuously.")
    }
}
