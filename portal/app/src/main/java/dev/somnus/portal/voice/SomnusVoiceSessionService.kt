package dev.somnus.portal.voice

import android.os.Bundle
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService

/**
 * The system's entry into the assistant. Registered as the device's
 * digital-assistant app, so every ROM assistant invocation reaches here:
 * long-press home, corner swipe, power-menu assistant, whatever the skin
 * maps (doc: the wake itself is the system's job, not ours - always-on
 * hotword detection via SoundTrigger is a later, hardware-gated layer).
 */
class SomnusVoiceSessionService : VoiceInteractionSessionService() {

    override fun onNewSession(args: Bundle?): SomnusVoiceSession {
        return SomnusVoiceSession(this)
    }
}
