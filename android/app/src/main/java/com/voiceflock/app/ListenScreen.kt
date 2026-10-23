package com.voiceflock.app

import android.content.Intent
import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.NotificationsActive
import androidx.compose.material.icons.rounded.PersonSearch
import androidx.compose.material.icons.rounded.Share
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle

@Composable
fun ListenScreen(openVoices: () -> Unit, openSettings: () -> Unit) {
    val ctx = LocalContext.current
    val listening by AppState.listening.collectAsStateWithLifecycle()
    val status by AppState.status.collectAsStateWithLifecycle()
    val det by AppState.detection.collectAsStateWithLifecycle()
    val level by AppState.level.collectAsStateWithLifecycle()
    val gallery by AppState.gallery.collectAsStateWithLifecycle()
    val sightings by AppState.sightings.collectAsStateWithLifecycle()
    val since by AppState.listeningSince.collectAsStateWithLifecycle()
    val speechS by AppState.speechSeconds.collectAsStateWithLifecycle()
    val prefs = remember { Prefs(ctx) }
    val g = gallery
    val thr = if (prefs.thresholdOverride > 0f) prefs.thresholdOverride else (g?.threshold ?: 0.75f)
    val start = rememberMicPermission { ListenService.start(ctx) }
    val identify = rememberMicPermission { ListenService.identify(ctx) }
    val ready = g != null && Gallery.modelFile(ctx).exists()
    val alertNow = det?.alert == true && listening

    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp, 8.dp, 16.dp, 24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("VoiceFlock", style = MaterialTheme.typography.headlineMedium, modifier = Modifier.weight(1f))
                if (listening) Pill("listening", VF.Ok, VF.Ok.copy(alpha = .14f)) else Pill("off")
            }
        }
        item {
            Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
                MicRing(level, listening, alertNow, enabled = ready) { if (listening) ListenService.stop(ctx) else start() }
                val line = when {
                    g == null -> "No voice library on this phone yet."
                    !Gallery.modelFile(ctx).exists() -> "Speaker model not downloaded yet."
                    listening -> "since ${Sightings.time(since)} · ${"%.0f".format(speechS)} s of speech heard"
                    status.startsWith("error") -> status
                    else -> "Tap to start listening. Alerts work with the screen off."
                }
                Text(line, color = VF.Muted, style = MaterialTheme.typography.bodyMedium, textAlign = TextAlign.Center)
                if (!ready) TextButton(onClick = openSettings) { Text(if (g == null) "Import a library →" else "Download the model →") }
            }
        }
        item { NowCard(det, listening, thr, g) }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(onClick = identify, enabled = ready, modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp)) {
                    Icon(Icons.Rounded.PersonSearch, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp)); Text("Identify a voice")
                }
                OutlinedButton(enabled = sightings.isNotEmpty(), modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp), onClick = {
                    val send = Intent(Intent.ACTION_SEND).setType("text/csv").putExtra(Intent.EXTRA_SUBJECT, "VoiceFlock sightings").putExtra(Intent.EXTRA_TEXT, Sightings.csv(sightings))
                    ctx.startActivity(Intent.createChooser(send, "Share sightings"))
                }) { Icon(Icons.Rounded.Share, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp)); Text("Share log") }
            }
        }
        item {
            Row(Modifier.fillMaxWidth().padding(top = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                Text("Heard", style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.width(8.dp)); if (sightings.isNotEmpty()) Pill("${sightings.size}")
                Spacer(Modifier.weight(1f))
                if (sightings.isNotEmpty()) TextButton(onClick = { Sightings.clear(ctx) }) { Text("Clear", color = VF.Muted) }
            }
        }
        if (sightings.isEmpty()) item { Empty(if (g == null) "Sightings appear here once a library is on the phone." else "Nothing heard yet. Known voices above the threshold are logged here, flagged ones also alert, and the laptop's detections appear too when it is connected.") }
        else {
            val days = sightings.groupBy { Sightings.dayLabel(it.at) }
            for ((day, list) in days) {
                item(key = "day-$day") { Label(day); }
                items(list, key = { "${it.at}-${it.personId}" }) { s -> SightingRow(s, thr) }
            }
        }
    }
}

@Composable
private fun SightingRow(s: Sighting, thr: Float) {
    RowItem(leading = { Avatar(s.personId, s.name, 38.dp, flagged = s.flagged) },
        trailing = {
            Column(horizontalAlignment = Alignment.End) {
                Text("${(s.probability * 100).toInt()}%", color = probColor(s.probability, thr), fontWeight = FontWeight.SemiBold)
                if (s.flagged) Icon(Icons.Rounded.NotificationsActive, null, tint = VF.Amber, modifier = Modifier.size(14.dp))
            }
        }) {
        TwoLine(s.name, Sightings.time(s.at) + (if (s.flagged) " · alerted" else "") + when (s.device) { "laptop" -> " · 💻 laptop"; "recording" -> " · 🎞 " + (s.source ?: "recording"); else -> "" })
    }
}

@Composable
fun NowCard(d: Detection?, listening: Boolean, thr: Float, g: Gallery?) {
    val alert = listening && d?.alert == true
    val bg by animateColorAsState(if (alert) VF.Amber.copy(alpha = .13f) else VF.Surface, label = "nowbg")
    val border by animateColorAsState(if (alert) VF.Amber.copy(alpha = .7f) else VF.Line, label = "nowbd")
    Surface(Modifier.fillMaxWidth(), shape = RoundedCornerShape(18.dp), color = bg, border = BorderStroke(1.dp, border)) {
        Column(Modifier.padding(16.dp).heightIn(min = 96.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            when {
                !listening -> { Label("Now"); Text("Not listening.", color = VF.Muted) }
                d == null -> { Label("Now"); Text("Warming up the speaker model…", color = VF.Muted) }
                !d.speech -> {
                    Label("Now")
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Box(Modifier.size(8.dp).clip(CircleShape).background(VF.Surface3))
                        Text("Quiet · waiting for speech", color = VF.Muted)
                        Spacer(Modifier.weight(1f))
                        Text("${d.levelDb.toInt()} dB", color = VF.Muted, style = MaterialTheme.typography.labelMedium)
                    }
                }
                d.top.isEmpty() -> { Label("Now"); Text("Speech, but the library is empty.", color = VF.Muted) }
                else -> {
                    val top = d.top.first()
                    Label(if (d.alert) "Flagged voice" else if (top.flagged) "Flagged · below threshold" else if (top.probability >= thr) "Recognised" else "Closest voice",
                        color = if (d.alert) VF.Amber else VF.Muted)
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        Avatar(top.person.id, top.person.name, 46.dp, flagged = top.flagged)
                        Column(Modifier.weight(1f)) {
                            Text(top.person.name, style = MaterialTheme.typography.displayMedium, maxLines = 1)
                            Text("${(top.probability * 100).toInt()}% · this window ${(top.instant * 100).toInt()}%", color = VF.Muted, style = MaterialTheme.typography.bodySmall)
                        }
                    }
                    ProbBar(top.probability, thr)
                    if (d.top.size > 1) Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        d.top.drop(1).forEach { Pill("${it.person.name} ${(it.probability * 100).toInt()}%") }
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun IdentifySheet(state: IdentifyState, onClose: () -> Unit) {
    val ctx = LocalContext.current
    val gallery by AppState.gallery.collectAsStateWithLifecycle()
    val g = gallery
    val prefs = remember { Prefs(ctx) }
    val thr = if (prefs.thresholdOverride > 0f) prefs.thresholdOverride else (g?.threshold ?: 0.75f)
    val dismiss = { ListenService.cancelIdentify(ctx); onClose() }
    ModalBottomSheet(onDismissRequest = dismiss, containerColor = VF.Surface, dragHandle = { BottomSheetDefaults.DragHandle(color = VF.Surface3) }) {
        Column(Modifier.padding(20.dp, 0.dp, 20.dp, 28.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            when (state) {
                is IdentifyState.Listening -> {
                    Text("Identify a voice", style = MaterialTheme.typography.headlineMedium)
                    Hint("Hold the phone toward whoever is speaking. Stops after 6 s of speech, or 15 s.")
                    Text("${"%.1f".format(state.speechS)} s", style = MaterialTheme.typography.displayLarge, color = VF.Amber)
                    ProbBar(state.speechS / 6f, 2f)
                    Hint("of speech collected · ${state.elapsedS.toInt()} s elapsed")
                    OutlinedButton(onClick = dismiss, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp)) { Text("Cancel") }
                }
                is IdentifyState.Failed -> {
                    Text("Couldn't identify", style = MaterialTheme.typography.headlineMedium)
                    Text(state.message)
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        OutlinedButton(onClick = onClose, modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp)) { Text("Close") }
                        Button(onClick = { ListenService.identify(ctx) }, modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp)) { Text("Try again") }
                    }
                }
                is IdentifyState.Result -> {
                    var newName by remember { mutableStateOf("") }
                    val best = state.top.firstOrNull()
                    Text(if (best != null && best.probability >= thr) "Sounds like ${best.person.name}" else "Who is this?", style = MaterialTheme.typography.headlineMedium)
                    Hint("${"%.1f".format(state.speechS)} s of speech, scored against ${g?.people?.size ?: 0} voices. Adding it teaches this phone now and the laptop after “Share my edits”.")
                    if (state.top.isEmpty()) Empty("The library is empty. Enroll this voice below.")
                    state.top.forEach { m ->
                        RowItem(leading = { Avatar(m.person.id, m.person.name, 40.dp) }, trailing = {
                            TextButton(onClick = {
                                LocalPrints.addToPerson(ctx, m.person.id, m.person.name, state.embedding); reloadGallery(ctx)
                                android.widget.Toast.makeText(ctx, "Added a print to ${m.person.name}", android.widget.Toast.LENGTH_SHORT).show(); onClose()
                            }) { Text("This is them") }
                        }) {
                            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                Text(m.person.name, fontWeight = FontWeight.Medium)
                                Text("${(m.probability * 100).toInt()}%", color = probColor(m.probability, thr), fontWeight = FontWeight.SemiBold)
                            }
                            ProbBar(m.probability, thr, Modifier.padding(top = 4.dp, end = 8.dp), 4.dp)
                        }
                    }
                    Divider()
                    Label("Someone new")
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                        OutlinedTextField(newName, { newName = it }, placeholder = { Text("Name") }, singleLine = true, modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp))
                        Button(enabled = newName.isNotBlank(), shape = RoundedCornerShape(12.dp), onClick = {
                            LocalPrints.addNew(ctx, newName, state.embedding); reloadGallery(ctx)
                            android.widget.Toast.makeText(ctx, "Enrolled ${newName.trim()} on this phone (alerts on)", android.widget.Toast.LENGTH_LONG).show(); onClose()
                        }) { Text("Enroll") }
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        OutlinedButton(onClick = { ListenService.identify(ctx) }, modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp)) { Text("Listen again") }
                        OutlinedButton(onClick = onClose, modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp)) { Text("Done") }
                    }
                }
            }
        }
    }
}
