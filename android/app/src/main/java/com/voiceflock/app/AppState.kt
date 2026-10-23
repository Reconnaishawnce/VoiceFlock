package com.voiceflock.app

import kotlinx.coroutines.flow.MutableStateFlow

/** Process-wide state shared between the foreground service and the UI. */
object AppState {
    val listening = MutableStateFlow(false)
    val status = MutableStateFlow("")
    val detection = MutableStateFlow<Detection?>(null)
    val log = MutableStateFlow<List<String>>(emptyList())
    val gallery = MutableStateFlow<Gallery?>(null)
    /** Microphone level 0..1, ten times a second while listening (drives the ring). */
    val level = MutableStateFlow(0f)
    val sightings = MutableStateFlow<List<Sighting>>(emptyList())
    val identify = MutableStateFlow<IdentifyState?>(null)
    val listeningSince = MutableStateFlow(0L)
    val speechSeconds = MutableStateFlow(0f)

    fun pushLog(line: String) {
        log.value = (listOf(line) + log.value).take(200)
    }
}
