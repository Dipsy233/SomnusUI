package dev.somnus.portal.voice

import android.service.voice.VoiceInteractionService

/**
 * Registers Somnus as the device's digital assistant (the slot MIUI/stock
 * Android fill with 小爱/Google). The system owns the wake; this class only
 * hands the system our session service. Nothing runs until woken.
 */
class SomnusVoiceService : VoiceInteractionService()
