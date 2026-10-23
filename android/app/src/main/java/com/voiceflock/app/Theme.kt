package com.voiceflock.app

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

/** Same palette as the web UI: warm charcoal surfaces, one amber signal colour, validated series colours. */
object VF {
    val Bg = Color(0xFF0F1114)
    val Surface = Color(0xFF171A1F)
    val Surface2 = Color(0xFF1E2229)
    val Surface3 = Color(0xFF262B33)
    val Fg = Color(0xFFECEBE6)
    val Fg2 = Color(0xFFC9CCD3)
    val Muted = Color(0xFF8D93A0)
    val Line = Color(0x1FFFFFFF)
    val Amber = Color(0xFFE8B35A)
    val AmberInk = Color(0xFF1A1304)
    val Blue = Color(0xFF7FB5FF)
    val Ok = Color(0xFF4FC08D)
    val Bad = Color(0xFFEF6F6F)
    private val palette = listOf(0xFF3987E5, 0xFFD95926, 0xFF199E70, 0xFFC98500, 0xFFD55181, 0xFF008300, 0xFF9085E9, 0xFFE66767).map { Color(it) }
    fun colorFor(id: Int): Color = palette[Math.floorMod(id * 7 + 3, palette.size)]
}

private val scheme = darkColorScheme(
    primary = VF.Amber, onPrimary = VF.AmberInk, primaryContainer = Color(0xFF3A2E12), onPrimaryContainer = VF.Amber,
    secondary = VF.Blue, onSecondary = Color(0xFF0B1A2E), secondaryContainer = Color(0xFF1C2A3F), onSecondaryContainer = VF.Blue,
    tertiary = VF.Ok, background = VF.Bg, onBackground = VF.Fg,
    surface = VF.Surface, onSurface = VF.Fg, surfaceVariant = VF.Surface2, onSurfaceVariant = VF.Muted,
    surfaceContainer = VF.Surface2, surfaceContainerHigh = VF.Surface3, surfaceContainerHighest = VF.Surface3,
    surfaceContainerLow = VF.Surface, surfaceContainerLowest = VF.Bg,
    outline = Color(0x33FFFFFF), outlineVariant = VF.Line, error = VF.Bad, onError = Color(0xFF2A0B0B),
)

private val type = Typography(
    displayLarge = TextStyle(fontFamily = FontFamily.Serif, fontWeight = FontWeight.Medium, fontSize = 40.sp, lineHeight = 44.sp, letterSpacing = (-0.8).sp),
    displayMedium = TextStyle(fontFamily = FontFamily.Serif, fontWeight = FontWeight.Medium, fontSize = 32.sp, lineHeight = 36.sp, letterSpacing = (-0.5).sp),
    headlineMedium = TextStyle(fontFamily = FontFamily.Serif, fontWeight = FontWeight.Medium, fontSize = 24.sp, lineHeight = 28.sp, letterSpacing = (-0.3).sp),
    titleLarge = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 19.sp, lineHeight = 24.sp),
    titleMedium = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 16.sp, lineHeight = 22.sp),
    titleSmall = TextStyle(fontWeight = FontWeight.Medium, fontSize = 14.sp, lineHeight = 20.sp),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 23.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp),
    bodySmall = TextStyle(fontSize = 12.5.sp, lineHeight = 17.sp),
    labelLarge = TextStyle(fontWeight = FontWeight.Medium, fontSize = 14.sp),
    labelMedium = TextStyle(fontWeight = FontWeight.Medium, fontSize = 12.sp, letterSpacing = 0.3.sp),
    labelSmall = TextStyle(fontWeight = FontWeight.SemiBold, fontSize = 10.5.sp, letterSpacing = 1.2.sp),
)

@Composable
fun VoiceFlockTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = scheme, typography = type, content = content)
}
