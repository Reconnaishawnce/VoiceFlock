package com.voiceflock.app

import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Inventory2
import androidx.compose.material.icons.rounded.Search
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle

private enum class Filter(val label: String) { ALL("All"), FLAGGED("Flagged"), PACKAGES("Packages") }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun VoicesScreen() {
    val ctx = LocalContext.current
    val prefs = remember { Prefs(ctx) }
    val gallery by AppState.gallery.collectAsStateWithLifecycle()
    var filter by rememberSaveable { mutableStateOf(Filter.ALL) }
    var query by rememberSaveable { mutableStateOf("") }
    var version by remember { mutableIntStateOf(0) }
    var editing by remember { mutableStateOf<Person?>(null) }
    var editingGroup by remember { mutableStateOf<Group?>(null) }
    val g = gallery
    editing?.let { p -> if (g != null) PersonSheet(p, g, prefs, onClose = { editing = null; version++ }) }
    editingGroup?.let { grp -> if (g != null) GroupSheet(grp, g, prefs, onClose = { editingGroup = null; version++ }) }

    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        Row(Modifier.fillMaxWidth().padding(top = 8.dp, bottom = 10.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("Voices", style = MaterialTheme.typography.headlineMedium, modifier = Modifier.weight(1f))
            if (g != null) key(version) { Pill("${g.people.count { prefs.isFlagged(it, g) }} flagged", VF.Amber, VF.Amber.copy(alpha = .14f)); Spacer(Modifier.width(6.dp)); Pill("${g.people.size} voices") }
        }
        if (g == null) { Empty("No library on this phone yet. Import one in Settings."); return }
        OutlinedTextField(query, { query = it }, placeholder = { Text("Search voices") }, leadingIcon = { Icon(Icons.Rounded.Search, null, tint = VF.Muted) },
            modifier = Modifier.fillMaxWidth(), singleLine = true, shape = RoundedCornerShape(14.dp))
        Row(Modifier.padding(vertical = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Filter.values().filter { it != Filter.PACKAGES || g.groups.isNotEmpty() }.forEach { f ->
                FilterChip(filter == f, { filter = f }, label = { Text(f.label + if (f == Filter.PACKAGES) " ${g.groups.size}" else "") }, shape = RoundedCornerShape(999.dp),
                    colors = FilterChipDefaults.filterChipColors(selectedContainerColor = VF.Surface3, selectedLabelColor = VF.Fg, labelColor = VF.Muted),
                    border = FilterChipDefaults.filterChipBorder(enabled = true, selected = filter == f, borderColor = VF.Line, selectedBorderColor = VF.Line))
            }
        }
        key(version) {
            if (filter == Filter.PACKAGES) LazyColumn(contentPadding = PaddingValues(bottom = 24.dp)) {
                item { Hint("One switch, sound and line for a whole group. A person's own settings win over their package.") }
                items(g.groups, key = { it.id }) { grp ->
                    RowItem(onClick = { editingGroup = grp },
                        leading = { Box(Modifier.size(40.dp).clip(CircleShape).background(VF.Surface3), contentAlignment = Alignment.Center) { Icon(Icons.Rounded.Inventory2, null, tint = VF.Fg2, modifier = Modifier.size(20.dp)) } },
                        trailing = { Switch(prefs.groupAlert(grp), { prefs.setGroupAlertOverride(grp.id, it); version++ }, colors = switchColors()) }) {
                        TwoLine(grp.name, "${grp.n} voices · ${prefs.groupSound(grp) ?: g.defaultSound}${if (prefs.groupMessage(grp) != null) " · own line" else ""}")
                    }
                    Divider()
                }
            } else LazyColumn(contentPadding = PaddingValues(bottom = 24.dp)) {
                item {
                    var alertAll by remember { mutableStateOf(prefs.alertAllKnown) }
                    Row(Modifier.fillMaxWidth().padding(bottom = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) { TwoLine("Alert on every known voice", "Otherwise only flagged voices and packages alert.") }
                        Switch(alertAll, { alertAll = it; prefs.alertAllKnown = it; version++ }, colors = switchColors())
                    }
                    Divider()
                }
                val list = g.people.filter { it.name.contains(query, true) }.filter { filter != Filter.FLAGGED || prefs.isFlagged(it, g) }
                    .sortedWith(compareBy({ !prefs.isFlagged(it, g) }, { it.kind != "named" }, { it.name.lowercase() }))
                if (list.isEmpty()) item { Empty(if (filter == Filter.FLAGGED) "Nothing flagged. Flip a switch to get an alert when that voice is heard." else "No voice matches.") }
                items(list, key = { it.id }) { p ->
                    val on = prefs.isFlagged(p, g)
                    val pkgs = p.groupIds.mapNotNull { g.groupById[it]?.name }
                    RowItem(onClick = { editing = p }, leading = { Avatar(p.id, p.name, 40.dp, flagged = on, photo = p.photo) },
                        trailing = { Switch(on, { prefs.setAlertOverride(p.id, it); version++ }, colors = switchColors()) }) {
                        val meta = buildList {
                            p.role?.let { add(it) }
                            add(if (p.local) "on this phone" else if (p.kind == "named") "known" else "unnamed")
                            add("${p.prints.size} print${if (p.prints.size == 1) "" else "s"}")
                            if (pkgs.isNotEmpty()) add(pkgs.joinToString(", "))
                            if (prefs.messageOverride(p.id) != null || p.alertMessage != null) add("own line")
                        }.joinToString(" · ")
                        TwoLine(p.name, meta)
                    }
                }
            }
        }
    }
}

@Composable
fun switchColors() = SwitchDefaults.colors(checkedThumbColor = VF.AmberInk, checkedTrackColor = VF.Amber, uncheckedThumbColor = VF.Muted, uncheckedTrackColor = VF.Surface3, uncheckedBorderColor = VF.Line)

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun SoundPicker(current: String, inheritLabel: String, onPick: (String) -> Unit) {
    val options = listOf("" to inheritLabel) + Gallery.SOUNDS.map { it.first to it.first }
    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        options.forEach { (v, label) ->
            FilterChip(current == v, { onPick(v) }, label = { Text(label) }, shape = RoundedCornerShape(999.dp),
                colors = FilterChipDefaults.filterChipColors(selectedContainerColor = VF.Amber.copy(alpha = .18f), selectedLabelColor = VF.Amber, labelColor = VF.Fg2),
                border = FilterChipDefaults.filterChipBorder(enabled = true, selected = current == v, borderColor = VF.Line, selectedBorderColor = VF.Amber.copy(alpha = .5f)))
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PersonSheet(p: Person, g: Gallery, prefs: Prefs, onClose: () -> Unit) {
    val ctx = LocalContext.current
    val sightings by AppState.sightings.collectAsStateWithLifecycle()
    var on by remember { mutableStateOf(prefs.isFlagged(p, g)) }
    var msg by remember { mutableStateOf(prefs.messageOverride(p.id) ?: p.alertMessage ?: "") }
    var snd by remember { mutableStateOf(prefs.soundOverride(p.id) ?: p.alertSound ?: "") }
    val inherited = p.groupIds.firstNotNullOfOrNull { gid -> g.groupById[gid]?.let { prefs.groupSound(it) } } ?: g.defaultSound
    val pkgs = p.groupIds.mapNotNull { g.groupById[it]?.name }
    val mine = sightings.filter { it.personId == p.id }.take(6)
    ModalBottomSheet(onDismissRequest = onClose, containerColor = VF.Surface, dragHandle = { BottomSheetDefaults.DragHandle(color = VF.Surface3) }) {
        Column(Modifier.padding(20.dp, 0.dp, 20.dp, 28.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                Avatar(p.id, p.name, 54.dp, flagged = on, photo = p.photo)
                Column(Modifier.weight(1f)) {
                    Text(p.name, style = MaterialTheme.typography.headlineMedium)
                    Hint(listOfNotNull(if (p.local) "enrolled on this phone" else if (p.kind == "named") "known voice" else "unnamed voice", "${p.prints.size} prints", pkgs.takeIf { it.isNotEmpty() }?.joinToString(", ")).joinToString(" · "))
                }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { TwoLine("Alert when heard", if (on) "Alarm, spoken line and a notification, even with the screen off." else "Only logged under Heard.") }
                Switch(on, { on = it; prefs.setAlertOverride(p.id, it) }, colors = switchColors())
            }
            Label("Alarm sound")
            SoundPicker(snd, "inherit ($inherited)") { snd = it }
            OutlinedTextField(msg, { msg = it }, label = { Text("Spoken line · [NAME] = ${p.name}") }, placeholder = { Text("empty = default") }, modifier = Modifier.fillMaxWidth(), minLines = 2, shape = RoundedCornerShape(12.dp))
            val fallback = p.groupIds.firstNotNullOfOrNull { gid -> g.groupById[gid]?.let { prefs.groupMessage(it) } } ?: prefs.defaultMessage.ifBlank { g.defaultMessage }
            Hint("Will say: “${Gallery.render(msg.ifBlank { fallback }, p.name)}”")
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp), onClick = {
                    Alerts.performAlert(ctx, p.name, snd.ifBlank { inherited }, Gallery.render(msg.ifBlank { fallback }, p.name), prefs.demoMode)
                }) { Text("▶ Test") }
                Button(modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp), onClick = { prefs.setMessageOverride(p.id, msg); prefs.setSoundOverride(p.id, snd); onClose() }) { Text("Save") }
            }
            if (mine.isNotEmpty()) {
                Divider(); Label("Recently heard")
                mine.forEach { s -> Row(Modifier.fillMaxWidth().padding(vertical = 3.dp)) { Text(Sightings.dayLabel(s.at) + " " + Sightings.time(s.at), color = VF.Muted, modifier = Modifier.weight(1f)); Text("${(s.probability * 100).toInt()}%" + if (s.flagged) " · alerted" else "", color = VF.Fg2) } }
            }
            if (p.local) {
                Divider()
                TextButton(onClick = { LocalPrints.remove(ctx, p.id); reloadGallery(ctx); Toast.makeText(ctx, "Removed ${p.name} from this phone", Toast.LENGTH_SHORT).show(); onClose() }) { Text("Remove this phone-enrolled voice", color = VF.Bad) }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun GroupSheet(grp: Group, g: Gallery, prefs: Prefs, onClose: () -> Unit) {
    val ctx = LocalContext.current
    var msg by remember { mutableStateOf(prefs.groupMessage(grp) ?: "") }
    var snd by remember { mutableStateOf(prefs.groupSound(grp) ?: "") }
    var on by remember { mutableStateOf(prefs.groupAlert(grp)) }
    ModalBottomSheet(onDismissRequest = onClose, containerColor = VF.Surface, dragHandle = { BottomSheetDefaults.DragHandle(color = VF.Surface3) }) {
        Column(Modifier.padding(20.dp, 0.dp, 20.dp, 28.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
                Box(Modifier.size(54.dp).clip(CircleShape).background(VF.Surface3), contentAlignment = Alignment.Center) { Icon(Icons.Rounded.Inventory2, null, tint = VF.Fg2) }
                Column(Modifier.weight(1f)) { Text(grp.name, style = MaterialTheme.typography.headlineMedium); Hint("package · ${grp.n} voices" + (grp.description?.let { " · $it" } ?: "")) }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { TwoLine("Alert on everyone in this package", "A person's own switch, sound or line overrides this.") }
                Switch(on, { on = it }, colors = switchColors())
            }
            Label("Alarm sound")
            SoundPicker(snd, "library default (${g.defaultSound})") { snd = it }
            OutlinedTextField(msg, { msg = it }, label = { Text("Spoken line for the package · [NAME] = the person") }, placeholder = { Text("empty = default") }, modifier = Modifier.fillMaxWidth(), minLines = 2, shape = RoundedCornerShape(12.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp), onClick = {
                    Alerts.performAlert(ctx, "Nancy Pelosi", snd.ifBlank { g.defaultSound }, Gallery.render(msg.ifBlank { prefs.defaultMessage.ifBlank { g.defaultMessage } }, "Nancy Pelosi"), prefs.demoMode)
                }) { Text("▶ Test") }
                Button(modifier = Modifier.weight(1f), shape = RoundedCornerShape(12.dp), onClick = { prefs.setGroupAlertOverride(grp.id, on); prefs.setGroupSoundOverride(grp.id, snd); prefs.setGroupMessageOverride(grp.id, msg); onClose() }) { Text("Save") }
            }
        }
    }
}
