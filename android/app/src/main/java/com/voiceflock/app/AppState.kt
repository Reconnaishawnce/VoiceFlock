package com.voiceflock.app

import kotlinx.coroutines.flow.MutableStateFlow

/** Process-wide state shared between the foreground service and the UI. */
object AppState {
    val listening = MutableStateFlow(false)
    val status = MutableStateFlow("")
    val detection = MutableStateFlow<Detection?>(null)
    val log = MutableStateFlow<List<String>>(emptyList())
    val gallery = MutableStateFlow<Gallery?>(null)

    fun pushLog(line: String) {
        log.value = (listOf(line) + log.value).take(200)
    }
}
