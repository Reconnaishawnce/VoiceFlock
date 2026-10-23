package com.voiceflock.app

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.*
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Mic
import androidx.compose.material.icons.rounded.Stop
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

@Composable
fun Section(title: String? = null, trailing: (@Composable () -> Unit)? = null, modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    Surface(modifier.fillMaxWidth(), shape = RoundedCornerShape(18.dp), color = VF.Surface, border = androidx.compose.foundation.BorderStroke(1.dp, VF.Line)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (title != null || trailing != null) Row(verticalAlignment = Alignment.CenterVertically) {
                if (title != null) Text(title, style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                else Spacer(Modifier.weight(1f))
                trailing?.invoke()
            }
            content()
        }
    }
}

@Composable
fun Label(text: String, color: Color = VF.Muted) {
    Text(text.uppercase(), style = MaterialTheme.typography.labelSmall, color = color)
}

@Composable
fun Hint(text: String) { Text(text, style = MaterialTheme.typography.bodySmall, color = VF.Muted) }

@Composable
fun Pill(text: String, color: Color = VF.Muted, bg: Color = VF.Surface3) {
    Text(text, style = MaterialTheme.typography.labelMedium, color = color,
        modifier = Modifier.background(bg, RoundedCornerShape(999.dp)).padding(horizontal = 9.dp, vertical = 3.dp))
}

fun initials(name: String): String {
    val parts = name.trim().split(Regex("\\s+")).filter { it.isNotEmpty() }
    if (parts.isEmpty()) return "?"
    if (parts[0].equals("Unknown", true) && parts.size > 1) return parts.last().filter { it.isDigit() }.ifEmpty { "U" }
    return (parts.first().take(1) + (parts.getOrNull(1)?.take(1) ?: "")).uppercase()
}

@Composable
fun Avatar(id: Int, name: String, size: Dp = 40.dp, flagged: Boolean = false) {
    val c = VF.colorFor(id)
    Box(contentAlignment = Alignment.Center) {
        Box(Modifier.size(size).clip(CircleShape).background(c.copy(alpha = .22f)), contentAlignment = Alignment.Center) {
            Text(initials(name), color = c, fontWeight = FontWeight.SemiBold, fontSize = (size.value * .36f).sp)
        }
        if (flagged) Box(Modifier.align(Alignment.BottomEnd).size(size * .34f).clip(CircleShape).background(VF.Surface).padding(2.dp)) {
            Box(Modifier.fillMaxSize().clip(CircleShape).background(VF.Amber))
        }
    }
}

fun probColor(p: Float, threshold: Float): Color = when {
    p >= threshold -> VF.Ok
    p >= threshold * .7f -> VF.Amber
    else -> VF.Muted
}

@Composable
fun ProbBar(p: Float, threshold: Float, modifier: Modifier = Modifier, height: Dp = 6.dp) {
    val w by animateFloatAsState(p.coerceIn(0f, 1f), animationSpec = tween(300), label = "prob")
    Box(modifier.fillMaxWidth().height(height).clip(RoundedCornerShape(999.dp)).background(VF.Surface3)) {
        Box(Modifier.fillMaxWidth(w).fillMaxHeight().clip(RoundedCornerShape(999.dp)).background(probColor(p, threshold)))
    }
}

@Composable
fun RowItem(modifier: Modifier = Modifier, onClick: (() -> Unit)? = null, leading: @Composable () -> Unit, trailing: (@Composable () -> Unit)? = null, content: @Composable ColumnScope.() -> Unit) {
    Row((if (onClick != null) modifier.clickable(onClick = onClick) else modifier).fillMaxWidth().padding(vertical = 9.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        leading()
        Column(Modifier.weight(1f), content = content)
        trailing?.invoke()
    }
}

@Composable
fun Divider() { HorizontalDivider(color = VF.Line) }

/** The hero control: level ring (amber, grows with the microphone level), pulsing halo while a flagged voice is heard. */
@Composable
fun MicRing(level: Float, listening: Boolean, alert: Boolean, enabled: Boolean, onClick: () -> Unit) {
    val lv by animateFloatAsState(if (listening) level.coerceIn(0f, 1f) else 0f, animationSpec = tween(120, easing = LinearEasing), label = "lv")
    val inf = rememberInfiniteTransition(label = "pulse")
    val pulse by inf.animateFloat(0f, 1f, infiniteRepeatable(tween(1400, easing = FastOutSlowInEasing), RepeatMode.Restart), label = "p")
    val ringColor by animateColorAsState(if (alert) VF.Amber else if (listening) VF.Ok else VF.Surface3, label = "rc")
    val fill by animateColorAsState(if (listening) VF.Surface3 else VF.Amber, label = "fill")
    Box(Modifier.size(224.dp), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            val stroke = 9.dp.toPx()
            val r = size.minDimension / 2 - stroke
            val c = Offset(size.width / 2, size.height / 2)
            if (listening && alert) {
                drawCircle(VF.Amber.copy(alpha = (1 - pulse) * .35f), radius = r + pulse * 26.dp.toPx(), center = c, style = Stroke(3.dp.toPx()))
            }
            drawCircle(VF.Surface3, radius = r, center = c, style = Stroke(stroke))
            if (listening) {
                drawArc(Brush.sweepGradient(listOf(ringColor.copy(alpha = .35f), ringColor), c), -90f, 360f * lv, false,
                    topLeft = Offset(c.x - r, c.y - r), size = Size(r * 2, r * 2), style = Stroke(stroke, cap = StrokeCap.Round))
            }
        }
        Box(Modifier.size(128.dp).clip(CircleShape).background(fill).clickable(enabled = enabled, onClick = onClick), contentAlignment = Alignment.Center) {
            Icon(if (listening) Icons.Rounded.Stop else Icons.Rounded.Mic, null, tint = if (listening) VF.Fg else VF.AmberInk, modifier = Modifier.size(52.dp))
        }
    }
}

@Composable
fun TwoLine(title: String, sub: String?, titleColor: Color = VF.Fg) {
    Text(title, fontWeight = FontWeight.Medium, color = titleColor, maxLines = 1, overflow = TextOverflow.Ellipsis)
    if (!sub.isNullOrEmpty()) Text(sub, style = MaterialTheme.typography.bodySmall, color = VF.Muted, maxLines = 2, overflow = TextOverflow.Ellipsis)
}

@Composable
fun Empty(text: String) {
    Column(Modifier.fillMaxWidth().padding(vertical = 26.dp), horizontalAlignment = Alignment.CenterHorizontally) {
        Canvas(Modifier.size(44.dp)) {
            val xs = listOf(.1f, .3f, .5f, .7f, .9f); val hs = listOf(.35f, .8f, 1f, .6f, .3f)
            for (i in xs.indices) drawLine(VF.Muted.copy(alpha = .5f), Offset(size.width * xs[i], size.height * (1 - hs[i]) / 2), Offset(size.width * xs[i], size.height * (1 + hs[i]) / 2), 4.dp.toPx(), StrokeCap.Round)
        }
        Spacer(Modifier.height(8.dp))
        Text(text, color = VF.Muted, style = MaterialTheme.typography.bodyMedium, textAlign = androidx.compose.ui.text.style.TextAlign.Center)
    }
}
